"""Host-side observation hooks for coding CLIs (issue #207).

CONTINUUM's recovery guarantees depend on the agent voluntarily calling
``continuum_record_progress`` / ``continuum_checkpoint``, which leaves an
unbounded window in which real work exists on disk but the event log knows
nothing about it. A kill inside that window hands the next session a contract
that understates what actually happened (observed live on 2026-08-22: a Claude
Code session wrote ``tic-tac-toe.html`` and was killed before any recording
call, so resume reported progress 0/1 with zero checkpoints).

The durable-execution literature (Temporal, Restate, LangGraph checkpointers,
the Crab sandbox paper) closes this by making recording mandatory at the
runtime layer rather than voluntary at the model layer. CONTINUUM cannot wrap
an external CLI's agent loop, but Claude Code exposes something nearly as
good: PostToolUse hooks fire after every Write/Edit completion, outside the
model's control. This module turns those hook events into durable evidence.

Two pieces live here:

- :func:`observe_event_payload` extracts the durable facts (tool, path, byte
  count, content digest) from one hook payload.
- :func:`install_claude_code_hook` / :func:`remove_claude_code_hook` manage the
  entry in ``.claude/settings.json`` that wires file-mutating tool completions
  to ``continuum observe``.

Provenance note: an observation is recorded ``Origin.EXTERNAL_AGENT``
deliberately. The fact it captures ("this tool call completed") is asserted by
the client harness, not verified by CONTINUUM, so it must never launder
self-reported state into trusted state. What it buys is independent *evidence*
a resumed session can weigh against the log.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "CLIENT_PROFILES",
    "DEFAULT_MATCHER",
    "observe_event_payload",
    "observe_command",
    "install_client_hook",
    "install_claude_code_hook",
    "remove_claude_code_hook",
]

#: Tool names whose completions carry a file path worth observing. Kept as one
#: matcher expression so the installed settings stay a single entry.
DEFAULT_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"

#: Every hook kind this module installs, which is also the final word of each
#: installed command. One list, read by both the installer and the remover: they
#: used to carry the set separately, and when the briefing hook was added
#: (7c6248d) only the remover's copy was updated, so the installer appended a
#: duplicate SessionStart group on every re-run instead of reporting it present
#: or repointing a moved one (issue #484, duplication fixed in #526). Keeping the
#: kinds here is what stops that drift recurring.
_INSTALLED_KINDS = ("observe", "gate", "briefing")

#: Keys of ``tool_input`` that hold the primary file path, in priority order.
_PATH_KEYS = ("file_path", "notebook_path")

#: Per-client wiring profiles (issue #209). Everything that differs between
#: clients is data, not code: which settings file they read, which hook
#: events exist, and which tools count as file-mutating there. The observe
#: and gate commands stay client-agnostic because every profiled client
#: speaks the same stdin contract (tool_name plus tool_input JSON).
CLIENT_PROFILES: dict[str, dict[str, str]] = {
    "claude-code": {
        "settings": ".claude/settings.json",
        "start_event": "SessionStart",
        "post_event": "PostToolUse",
        "pre_event": "PreToolUse",
        "write_matcher": "Write|Edit|MultiEdit|NotebookEdit",
        "any_matcher": "*",
    },
    "gemini": {
        "settings": ".gemini/settings.json",
        "start_event": "SessionStart",
        "post_event": "AfterTool",
        "pre_event": "BeforeTool",
        "write_matcher": "write_file|replace",
        "any_matcher": ".*",
    },
    "codex": {
        "settings": ".codex/hooks.json",
        "start_event": "SessionStart",
        "post_event": "PostToolUse",
        "pre_event": "PreToolUse",
        # Documented surface as of mid-2026: Codex hooks fire for shell/Bash
        # calls only; apply_patch and MCP tools do not traverse them.
        "write_matcher": "^Bash$|^shell$",
        "any_matcher": "^Bash$|^shell$",
    },
}


def observe_event_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the durable facts from one PostToolUse payload.

    Returns a JSON-native dict suitable for a ``TOOL_COMPLETED`` event:
    ``{"tool": ..., "path": ..., "bytes": ..., "sha256": ...}``. The path keys
    are read from the hook's own report; size and digest come from reading the
    file *now*, because the observation is only useful to recovery if it
    describes what is actually on disk. A missing or unreadable file records
    the path without size or digest: the absence is itself evidence, and
    guessing values would poison it.
    """
    tool = str(raw.get("tool_name") or "unknown")
    payload: dict[str, Any] = {"tool": tool}

    tool_input = raw.get("tool_input")
    path: str | None = None
    if isinstance(tool_input, Mapping):
        for key in _PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                path = value
                break
    if path is None:
        return payload

    payload["path"] = path
    # Read in bounded chunks rather than whole: the hook runs after every
    # file-mutating tool call, and a multi-gigabyte artifact must not be held
    # in memory just to hash it. Size and digest are only recorded after the
    # read completes, so a mid-read failure records neither.
    digest = hashlib.sha256()
    size = 0
    try:
        with Path(path).open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        return payload
    payload["bytes"] = size
    payload["sha256"] = digest.hexdigest()
    try:
        from continuum.environment.file_snapshot import snapshot_file

        snapshot_file(path, sha256=payload["sha256"])
    except Exception:
        pass
    return payload


def _join_command(parts: list[str]) -> str:
    """Join an argv list into a string the host's shell will parse back.

    The two shell families disagree on quoting, and the hook command is handed
    to whichever one the client uses. POSIX shells read shlex's single quotes;
    ``cmd.exe`` has no single-quote syntax at all and wants double quotes.
    Windows paths routinely contain spaces, so quoting one the POSIX way
    leaves ``cmd.exe`` trying to run a program called ``'D:\\pROJ`` and every
    installed hook dies silently. ``subprocess.list2cmdline`` is the quoting
    convention the Windows C runtime itself parses.
    """
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def _split_command(command: str) -> list[str]:
    """Split a hook command back into argv: the inverse of :func:`_join_command`.

    On Windows shlex's POSIX mode treats the backslashes in a path as escape
    characters, so an unquoted ``C:\\Py\\Scripts\\continuum.exe`` comes back as
    ``C:PyScriptscontinuum.exe`` and the recogniser stops seeing its own
    commands. ``posix=False`` keeps them, at the cost of leaving the quotes
    attached to the token. Both quote characters are stripped so a command
    written by an older version -- POSIX-quoted even on Windows -- is still
    recognised, which is what lets ``hooks remove`` clean up after an upgrade.
    """
    if os.name == "nt":
        return [token.strip("\"'") for token in shlex.split(command, posix=False)]
    return shlex.split(command)


def observe_command(*, db: str | None = None) -> str:
    """Build the shell command the hook will run.

    The absolute path of the ``continuum`` executable is resolved at install
    time and baked into the settings: hook processes may not inherit the PATH
    that found the binary originally. When no executable is on PATH (editable
    installs inside environments that expose only the interpreter), the
    interpreter-plus-module form is used instead. An explicit ``db`` is baked
    in too, since the default resolves relative to the working directory and
    hook processes run with the project root as cwd.
    """
    executable = shutil.which("continuum")
    parts = [executable] if executable else [sys.executable, "-m", "continuum.cli"]
    if db:
        parts += ["--db", db]
    parts.append("observe")
    return _join_command(parts)


def _is_continuum_hook(hook: Mapping[str, Any], kind: str) -> bool:
    """True when a hook entry is one this module would have installed.

    Deliberately narrow: a command that merely ends in the kind word could
    belong to an unrelated tool, and treating it as ours would let install
    repoint or remove delete someone else's configuration. Two shapes are
    recognised, matching :func:`observe_command` exactly: a resolved
    ``continuum`` executable path (its stem is ``continuum``), and the
    interpreter fallback form ``<python> -m continuum.cli ... <kind>``.
    """
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = _split_command(command)
    except ValueError:
        return False
    if len(tokens) < 2 or tokens[-1] != kind:
        return False
    if Path(tokens[0]).stem == "continuum":
        return True
    return tokens[1] == "-m" and len(tokens) >= 4 and tokens[2] == "continuum.cli"


def _is_observe_hook(hook: Mapping[str, Any]) -> bool:
    return _is_continuum_hook(hook, "observe")


def _installed_kind(command: str) -> str | None:
    """Which of :data:`_INSTALLED_KINDS` ``command`` ends in, or ``None``.

    The kind a command carries is what makes an existing entry the same hook
    rather than a different one, so it is read from the command being installed
    instead of being listed at the call site: a new kind then cannot reach the
    installer without the installer knowing about it. ``None`` for anything else,
    which leaves :func:`_install_hook` appending, exactly as it does today for a
    command this module did not build.
    """
    try:
        tokens = _split_command(command)
    except ValueError:
        return None
    if tokens and tokens[-1] in _INSTALLED_KINDS:
        return tokens[-1]
    return None


def install_client_hook(
    settings_path: Path,
    command: str,
    *,
    event_name: str,
    matcher: str,
) -> str:
    """Install one continuum hook entry into any client's settings file.

    The client-agnostic core behind every installer (#209): add (or repoint,
    or recognise as present) a single entry under ``hooks.<event_name>``.
    Returns "installed", "updated" or "present", matching the claude-code
    contract this was extracted from.
    """
    return _install_hook(settings_path, command, event_name=event_name, matcher=matcher)


def install_claude_code_hook(
    settings_path: Path,
    command: str,
    *,
    kind: str = "observe",
    matcher: str | None = None,
) -> str:
    """Claude Code wrapper; see :func:`install_client_hook`."""
    if matcher is None:
        matcher = DEFAULT_MATCHER if kind == "observe" else "*"
    event_name = "PostToolUse" if kind == "observe" else "PreToolUse"
    return _install_hook(settings_path, command, event_name=event_name, matcher=matcher)


def _install_hook(
    settings_path: Path,
    command: str,
    *,
    event_name: str,
    matcher: str,
) -> str:
    """Add a continuum hook entry to a client settings file.

    Existing settings are preserved; only the matching list under ``hooks``
    gains (or updates) our single entry of the kind ``command`` names.
    Returns ``"installed"`` when the entry was added, ``"updated"`` when an
    existing entry of the same kind pointed somewhere else (a moved
    virtualenv, say) and was repointed, ``"present"`` when nothing needed to
    change.

    An entry counts as ours only when its kind matches the one being installed,
    so an observe install cannot repoint a briefing or gate hook that happens to
    share the event and matcher: repointing across kinds would silently drop a
    hook the caller never asked about.

    A settings file that exists but is unreadable raises rather than being
    overwritten: a file someone edited by hand is a statement of intent, and
    silently replacing it would destroy work to save a typo.
    """
    if settings_path.exists():
        try:
            settings: dict[str, Any] = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{settings_path} is not valid JSON ({exc}); refusing to edit it"
            ) from exc
        if not isinstance(settings, dict):
            raise ValueError(f"{settings_path} does not contain a JSON object")
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{settings_path}: 'hooks' is not an object")

    hook_list: list[Any] = hooks.setdefault(event_name, [])
    if not isinstance(hook_list, list):
        raise ValueError(f"{settings_path}: 'hooks.{event_name}' is not a list")

    status = "installed"
    entry_found = False
    kind = _installed_kind(command)
    for group in hook_list:
        if not isinstance(group, dict) or group.get("matcher") != matcher:
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        for hook in entries:
            ours = kind is not None and isinstance(hook, dict) and _is_continuum_hook(hook, kind)
            if ours:
                entry_found = True
                if hook.get("command") != command:
                    hook["command"] = command
                    status = "updated"
                else:
                    status = "present"

    if not entry_found:
        hook_list.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command}],
            }
        )

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return status


def remove_claude_code_hook(settings_path: Path) -> bool:
    """Remove every continuum hook this module installed. True when anything
    was removed.

    Only entries this module's shape recognises are touched
    (:data:`_INSTALLED_KINDS`, any matcher): a hand-written entry pointing
    elsewhere survives untouched, as does every other key in the file. A group
    holding unrelated hooks keeps them.
    """
    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{settings_path} is not valid JSON ({exc}); refusing to edit it") from exc
    if not isinstance(settings, dict):
        return False

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False

    removed = False
    # Scan every event list, not just the Claude Code names: clients differ
    # in what they call their hook points (Gemini uses AfterTool/BeforeTool).
    for event_name in [k for k, v in hooks.items() if isinstance(v, list)]:
        hook_list = hooks[event_name]
        if not isinstance(hook_list, list):
            continue

        kept_groups: list[Any] = []
        for group in hook_list:
            if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)):
                kept_groups.append(group)
                continue
            # Drop only the hook entries this module recognises as its own. A
            # matcher group can hold unrelated user hooks alongside ours;
            # removing the whole group would delete configuration this command
            # never installed.
            kept_hooks = [
                h
                for h in group["hooks"]
                if not (
                    isinstance(h, dict) and any(_is_continuum_hook(h, k) for k in _INSTALLED_KINDS)
                )
            ]
            if len(kept_hooks) != len(group["hooks"]):
                removed = True
            if not kept_hooks:
                continue
            group["hooks"] = kept_hooks
            kept_groups.append(group)

        # Rewrite each list unconditionally: keeping only recognised-ours
        # entries and surviving groups is idempotent whether or not this run
        # removed anything.
        if kept_groups:
            hooks[event_name] = kept_groups
        elif event_name in hooks:
            del hooks[event_name]

    if not removed:
        return False

    if not hooks:
        del settings["hooks"]

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True

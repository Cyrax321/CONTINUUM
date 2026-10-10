"""Does CONTINUUM actually work in the IDE I am in right now?

The question this answers is the one the whole program exists to serve, and
it is deliberately narrower than "is my database fine". A registration can be
present, current and still useless in three separate ways, and each has a
different fix:

* **not wired**: no config file names CONTINUUM at all. Nothing is broken;
  the IDE simply has not been set up. This is not a failure of the program and
  is never reported as one.
* **stale**: a path baked at install time no longer resolves, which is what a
  deleted or renamed virtualenv looks like from here. The host cannot spawn
  anything, so every tool is missing.
* **read-only-degraded**: the server starts, lists its read-only tools, and
  refuses every mutating one. Nothing is visibly broken until an agent tries
  to record something, which is why it is worth naming explicitly.

The third is the quiet one. It is a pure consequence of the authorization
policy, so this module asks :mod:`continuum.mcp.authz` which client names that
policy permits and compares them against the name each registration declares.
It does not evaluate the policy itself, and it does not re-derive what
:mod:`continuum.mcp.doctor` already proves about the server: the wire-level
handshake is that module's job and is folded in by ``--deep`` rather than
being approximated here.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..clienthooks import CLIENT_PROFILES
from ..mcp.authz import (
    POLICY_ENV_VAR,
    POLICY_ENV_VAR_ALIAS,
    UNKNOWN_CALLER,
    AuthorizationPolicy,
    load_policy,
)
from ..mcp.install import HOST_PROFILES, SERVER_NAME
from . import generator
from .targets import TARGETS

__all__ = [
    "DoctorReport",
    "IdeReport",
    "WIRED",
    "STALE",
    "DEGRADED",
    "NOT_CONFIGURED",
    "scan",
    "render_report",
]

WIRED = "wired"
STALE = "stale"
DEGRADED = "read-only-degraded"
NOT_CONFIGURED = "not-configured"
CURRENT = "current"

#: Commands the hook clients install, keyed by the trailing word of the
#: invocation. Used only to recognise a continuum hook in a settings file.
_HOOK_KINDS = ("observe", "gate", "briefing", "precompact")


# --------------------------------------------------------------------------- #
# config reading
# --------------------------------------------------------------------------- #


def _read_config(path: Path) -> Any | None:
    """Parse a host config file, or ``None`` when it cannot be read.

    Unreadable is deliberately not an error: a doctor that dies because one
    IDE's settings file has a stray comma is worse than one that reports the
    file as unparseable. The caller records that as a detail.

    Format handling is json, toml and yaml, matching the hosts that use each.
    ``mcp.configfmt`` is the single reader for this once the format-adapter
    track merges; until then this mirrors it rather than importing it, so
    neither module owns the other's behaviour.
    """

    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(text)
        if suffix == ".toml":
            import tomllib

            return tomllib.loads(text)
        if suffix in (".yaml", ".yml"):
            # Optional dependency with no stubs, imported lazily because a
            # clone without the yaml extra must still be able to run the
            # doctor. Scoped ignore rather than a blanket one: if PyYAML ever
            # gains stubs, the next reader finds a real error instead of a
            # silenced one.
            import yaml  # type: ignore[import-untyped]

            return yaml.safe_load(text)
    except Exception:
        return None
    # A settings file with no extension (Codex's hooks.json aside) is JSON in
    # every profile this project carries.
    if not suffix:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield every ``(dotted path, value)`` pair in a parsed config."""

    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, value
            yield from _walk(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = f"{path}[{index}]"
            yield child, value
            yield from _walk(value, child)


def _expand(raw: str, *, root: Path, home: Path) -> Path:
    """Resolve a profile path, which may start with ``~`` or be relative."""

    text = os.path.expandvars(raw)
    if text.startswith("~"):
        text = str(home) + text[1:]
    path = Path(text).expanduser()
    return path if path.is_absolute() else root / path


def _resolves(command: str, *, home: Path) -> bool:
    """Whether a baked command still resolves the way a host would find it.

    An absolute path must exist. A bare name is resolved against PATH, which
    is what the host does, so a command that works from the user's shell is
    not reported stale by a doctor that only looked at the filesystem.
    """

    path = Path(command).expanduser()
    if path.is_absolute():
        return path.exists()
    return shutil.which(command, path=str(home / ".local" / "bin")) is not None or (
        shutil.which(command) is not None
    )


def _declared_clients(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Client names the registration itself grants, from its ``env`` block."""

    env = entry.get("env")
    if not isinstance(env, Mapping):
        return ()
    for var in (POLICY_ENV_VAR_ALIAS, POLICY_ENV_VAR):
        raw = env.get(var)
        if isinstance(raw, str) and raw.strip():
            return tuple(part for part in raw.replace(",", " ").split() if part)
    return ()


# --------------------------------------------------------------------------- #
# report shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IdeReport:
    """One IDE's wiring state, across every surface it reads."""

    ide: str
    surface: str
    """``mcp``, ``hooks`` or ``instructions``."""

    wired: bool
    stale: bool
    degraded: bool
    summary: str
    config_paths: tuple[str, ...] = ()
    found_in: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    remedy: str | None = None
    clients: tuple[str, ...] = ()
    state_override: str | None = None
    """Set when the surface's own vocabulary is more precise than the generic one.

    An instruction target distinguishes current, drifted, unmanaged and
    absent, which are four genuinely different situations. Collapsing them
    into wired/not-wired would lose the distinction the generator exists to
    make, so the row carries its own word and only falls back to the generic
    state when there is none.
    """

    @property
    def state(self) -> str:
        """The single word a human reads, worst news first.

        Stale outranks degraded because a stale registration stops the server
        from starting at all, whereas a degraded one still answers read-only
        queries. Both are true at once often enough to need naming both.
        """

        if self.state_override is not None:
            return self.state_override
        if self.stale and self.degraded:
            return f"{STALE}+{DEGRADED}"
        if self.stale:
            return STALE
        if self.degraded:
            return DEGRADED
        if self.wired:
            return WIRED
        return NOT_CONFIGURED

    def to_dict(self) -> dict[str, Any]:
        return {
            "ide": self.ide,
            "surface": self.surface,
            "state": self.state,
            "wired": self.wired,
            "stale": self.stale,
            "degraded": self.degraded,
            "summary": self.summary,
            "config_paths": list(self.config_paths),
            "found_in": list(self.found_in),
            "details": list(self.details),
            "remedy": self.remedy,
            "clients": list(self.clients),
        }


@dataclass(frozen=True)
class DoctorReport:
    """Every IDE checked, plus the optional server-level verdict."""

    ides: tuple[IdeReport, ...] = ()
    server: dict[str, Any] | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def stale(self) -> tuple[IdeReport, ...]:
        return tuple(i for i in self.ides if i.stale)

    @property
    def degraded(self) -> tuple[IdeReport, ...]:
        return tuple(i for i in self.ides if i.degraded)

    @property
    def healthy(self) -> bool:
        """Whether anything checked needs a human to act.

        Unconfigured IDEs do not count against health: an IDE nobody has
        wired yet is the normal state of a machine, and reporting it as a
        problem would make the command useless as a health check.
        """

        return not self.stale and not self.degraded and self._server_healthy

    @property
    def _server_healthy(self) -> bool:
        return self.server is None or bool(self.server.get("healthy", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": "continuum doctor",
            "healthy": self.healthy,
            "stale": [i.ide for i in self.stale],
            "degraded": [i.ide for i in self.degraded],
            "ides": [i.to_dict() for i in self.ides],
            "server": self.server,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #


def _scan_mcp_host(
    host: str, *, root: Path, home: Path, policy: AuthorizationPolicy
) -> IdeReport:
    """Check one MCP host across every scope its profile names."""

    profile = HOST_PROFILES[host]
    expected = profile.get("mutating_clients", host)
    paths = {
        key: _expand(profile[key], root=root, home=home)
        for key in ("project_settings", "local_settings", "user_settings")
        if key in profile
    }

    found_in: list[str] = []
    details: list[str] = []
    stale_paths: list[str] = []
    granted: set[str] = set()

    for label, path in paths.items():
        data = _read_config(path)
        if data is None:
            if path.is_file():
                details.append(f"{label}: {path} could not be parsed")
            continue
        for dotted, value in _walk(data):
            if not dotted.endswith(f".{SERVER_NAME}") or not isinstance(value, Mapping):
                continue
            found_in.append(f"{label}: {path}")
            granted.update(_declared_clients(value))
            command = value.get("command")
            if isinstance(command, str) and not _resolves(command, home=home):
                stale_paths.append(f"{label}: {path} bakes {command}")
                details.append(f"{label}: {path} bakes {command}, which no longer resolves")
            elif isinstance(command, str):
                details.append(f"{label}: {path} -> {command}")
            args = value.get("args")
            if isinstance(args, list) and args:
                details.append(f"{label}: args {' '.join(str(a) for a in args)}")

    # The effective grant is what the server will see: the registration's own
    # env block when it carries one, otherwise whatever policy the operator
    # has configured. Either way the question is asked of authz, never
    # re-decided here.
    permitted = granted or set(policy.allowed)
    degraded = bool(found_in) and not permitted
    clients = tuple(sorted(permitted)) if permitted else (UNKNOWN_CALLER,)

    if degraded:
        details.append(
            f"read-only: {host} expects client {expected!r} but no allow-list grants it "
            f"(permitted: {', '.join(clients) if clients else 'none'})"
        )

    return IdeReport(
        ide=host,
        surface="mcp",
        wired=bool(found_in),
        stale=bool(stale_paths),
        degraded=degraded,
        summary=_summarise(found_in, stale_paths, degraded, host),
        config_paths=tuple(str(p) for p in paths.values()),
        found_in=tuple(found_in),
        details=tuple(details),
        remedy=(
            f"re-run `continuum mcp install --host {host}` to re-bake the path"
            if stale_paths
            else (
                f"grant writes with {POLICY_ENV_VAR_ALIAS}={expected!r} "
                f"(or `continuum mcp install --host {host}`)"
                if degraded
                else None
            )
        ),
        clients=clients,
    )


def _summarise(
    found_in: Sequence[str], stale_paths: Sequence[str], degraded: bool, host: str
) -> str:
    if stale_paths:
        return f"{host}: wired but stale in {len(stale_paths)} file(s)"
    if not found_in:
        return f"{host}: nothing configured"
    if degraded:
        return f"{host}: wired but read-only-degraded"
    return f"{host}: wired in {len(found_in)} file(s)"


def _scan_hook_client(client: str, *, root: Path, home: Path) -> IdeReport:
    """Check one hook client's settings file for a continuum hook."""

    profile = CLIENT_PROFILES[client]
    path = _expand(profile["settings"], root=root, home=home)
    data = _read_config(path)
    found = False
    details: list[str] = []
    stale: list[str] = []
    stale_command = ""

    if data is not None:
        for dotted, value in _walk(data):
            if not isinstance(value, str) or "continuum" not in value:
                continue
            tokens = value.split()
            if not tokens or "continuum" not in Path(tokens[0]).name:
                continue
            kind = next((k for k in _HOOK_KINDS if k in tokens), None)
            if kind is None:
                continue
            found = True
            details.append(f"{dotted}: {kind} hook")
            if not _resolves(tokens[0], home=home):
                stale.append(f"{path} bakes {tokens[0]}")
                stale_command = tokens[0]

    if stale:
        summary = f"{client}: hook command no longer resolves ({stale_command})"
    elif found:
        summary = f"{client}: {len(details)} continuum hook(s)"
    else:
        summary = f"{client}: nothing configured"

    return IdeReport(
        ide=client,
        surface="hooks",
        wired=found,
        stale=bool(stale),
        degraded=False,
        summary=summary,
        config_paths=(str(path),),
        found_in=(str(path),) if found else (),
        details=tuple(details),
        remedy=(
            f"re-run `continuum hooks install {client}` to re-bake the path" if stale else None
        ),
    )


def _scan_instructions(root: Path) -> list[IdeReport]:
    """Check the instruction targets this generator owns."""

    reports = []
    for target in TARGETS.values():
        result = generator.check(target, root=root)
        wired = result.state in ("current", "drifted")
        stale = result.state == "drifted"
        unmanaged = result.state == "unmanaged"
        # "absent" is the instruction surface's way of saying nothing is
        # configured, which is the same plain, non-failing statement the MCP
        # rows make.
        state = NOT_CONFIGURED if result.state == "absent" else result.state
        reports.append(
            IdeReport(
                ide=target.id,
                surface="instructions",
                wired=wired or unmanaged,
                stale=stale,
                degraded=False,
                state_override=state,
                summary=(
                    f"{target.path}: {result.state}"
                    if result.state != "absent"
                    else f"{target.path}: not generated"
                ),
                config_paths=(str(result.path),),
                found_in=(str(result.path),) if (wired or unmanaged) else (),
                details=(result.detail,),
                remedy=(
                    f"run `continuum agents install --target {target.id}`"
                    if stale
                    else None
                ),
            )
        )
    return reports


def scan(
    *,
    root: Path | None = None,
    home: Path | None = None,
    deep: bool = False,
    timeout: float = 15.0,
) -> DoctorReport:
    """Scan every known host, client and instruction target.

    ``deep`` additionally runs the MCP server's own handshake checks from
    :mod:`continuum.mcp.doctor`. They are off by default because each one
    spawns a subprocess and the last performs a real initialize round-trip,
    which is the right cost for ``--deep`` and the wrong one for a status line.
    """

    base = Path.cwd() if root is None else root
    user_home = Path(os.path.expanduser("~")) if home is None else home
    policy = load_policy(root=base)

    reports = [_scan_mcp_host(host, root=base, home=user_home, policy=policy) for host in HOST_PROFILES]
    reports += [_scan_hook_client(client, root=base, home=user_home) for client in CLIENT_PROFILES]
    reports += _scan_instructions(base)

    server: dict[str, Any] | None = None
    if deep:
        from ..mcp.doctor import run_doctor

        server = run_doctor(timeout=timeout)

    return DoctorReport(ides=tuple(reports), server=server)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

_WIDTH = 15


def render_report(report: DoctorReport) -> str:
    """One actionable line per IDE, then the verdict and what to do."""

    lines = ["CONTINUUM doctor: is CONTINUUM actually wired up here?", ""]
    for ide in report.ides:
        lines.append(f"{ide.ide:<{_WIDTH}} {ide.surface:<13} {ide.state:<20} {ide.summary}")
        if ide.state in (STALE, DEGRADED, f"{STALE}+{DEGRADED}"):
            for detail in ide.details:
                if "read-only" in detail or "bakes" in detail:
                    lines.append(f"{'':<{_WIDTH}} -> {detail}")
            if ide.remedy:
                lines.append(f"{'':<{_WIDTH}} -> fix: {ide.remedy}")
    lines.append("")
    if report.healthy:
        lines.append("healthy: every wired IDE is current and authorized.")
    else:
        lines.append("not healthy: see the stale and read-only-degraded lines above.")
    if report.server is not None:
        lines.append(
            "server: "
            + ("handshake ok." if report.server.get("healthy") else "handshake failed; see `continuum mcp doctor`.")
        )
    unconfigured = [i.ide for i in report.ides if not i.wired]
    if unconfigured:
        lines.append(f"not configured (not a failure): {', '.join(unconfigured)}")
    return "\n".join(lines)

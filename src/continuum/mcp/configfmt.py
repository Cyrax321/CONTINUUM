"""Reading and writing the config files MCP hosts keep their servers in.

Two layers live here, and the point of separating them is that they vary
independently. A host's *format* (JSON, TOML, YAML) says how bytes become
values. A host's *shape* (a dict keyed by server name, or a list of records)
says where the registration sits inside those values. Codex is TOML and
dict-shaped; Continue is YAML and list-shaped; opencode is JSON, dict-shaped,
and spells the command as one argv array rather than ``command`` plus ``args``.
Every combination has to work, so neither layer may branch on the other.

Why the codecs are not all in the standard library
-------------------------------------------------

``install.py`` is pure standard library on purpose: it has to work in the
state where the ``mcp`` extra is missing, and a parser that is itself an
extra would make ``mcp install`` fail for a reason that has nothing to do with
MCP. Reading TOML is therefore free (``tomllib``, 3.11+), but *writing* TOML
has no standard-library writer, and YAML has no standard-library parser at
all. Both are imported lazily and their absence is reported as an actionable
refusal, the same discipline ``continuum.adapters.thin`` uses for CrewAI and
AutoGen, rather than silently becoming a hard dependency.

The refusal to rewrite is the other half of that contract. These files belong
to somebody else's editor and hold configuration this tool knows nothing
about. Every format that can carry comments (TOML, YAML) or formatting a
dumper would normalise is checked before a write: a file carrying comments is
reported rather than rewritten, because a full re-serialisation is exactly how
a user's annotated config gets flattened into something they did not write.

Sources for every host profile in ``install.HOST_PROFILES`` are recorded next
to the profile itself.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "FORMATS",
    "ConfigError",
    "read_document",
    "render_document",
    "write_document",
]


class ConfigError(ValueError):
    """A config file this module refuses to touch, and why.

    A ``ValueError`` because that is what the install path already raised for
    an unreadable or foreign settings file, and the CLI reports the message
    to the operator either way.
    """


#: Formats this module can read and write, keyed by the name a profile uses.
FORMATS = ("json", "toml", "yaml")

#: How each format describes a document that is not a mapping at all. The
#: wording is per-format on purpose: the existing JSON refusal is part of the
#: documented contract for ``mcp install``, so it keeps its own phrasing.
_ROOT_LABEL = {
    "json": "a JSON object",
    "toml": "a TOML table",
    "yaml": "a YAML mapping",
}

#: What to tell an operator whose file parses but whose top level is a list.
#: Kept next to :data:`_ROOT_LABEL` because both are refusal copy, not logic.
_ROOT_CAUSE = {"json": "JSON", "toml": "TOML", "yaml": "YAML"}


# --------------------------------------------------------------------------- #
# optional parsers
# --------------------------------------------------------------------------- #


def _toml_writer() -> Any:
    """The TOML dumper, imported lazily, or a refusal naming the install.

    ``tomllib`` reads TOML and the standard library has no writer to match it,
    so a write needs an extra. Rather than make every ``continuum`` install
    carry one, the missing parser is reported the way ``mcp install`` reports a
    missing SDK: by name, with the command that fixes it.
    """
    try:
        # Resolved by name rather than ``import tomli_w`` so the optional
        # package stays out of the module's import graph: a type checker must
        # be able to read this file on a machine that has never installed it.
        return importlib.import_module("tomli_w")
    except ImportError as exc:
        raise ConfigError(
            "writing TOML needs the 'tomli-w' package, which is not installed. "
            'Install it with: pip install "continuum-agent[mcp]"'
        ) from exc


def _yaml() -> Any:
    """The YAML module, imported lazily, or a refusal naming the install."""
    try:
        return importlib.import_module("yaml")
    except ImportError as exc:
        raise ConfigError(
            "reading and writing YAML needs the 'PyYAML' package, which is not "
            'installed. Install it with: pip install "continuum-agent[mcp]"'
        ) from exc


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #


def read_document(fmt: str, path: Path) -> dict[str, Any]:
    """Read ``path`` as ``fmt``, returning its top-level mapping.

    A file that is absent reads as an empty mapping, which is how an editor
    treats a config it has not written yet. A file that exists but does not
    parse, or parses into something that is not a mapping, raises
    :class:`ConfigError` instead of being replaced: silently recreating a file
    a typo broke would trade a one-line fix for a whole settings file gone.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return _parse(fmt, text, path)


def _parse(fmt: str, text: str, path: Path) -> dict[str, Any]:
    """Parse ``text`` in ``fmt``, refusing anything that is not a mapping."""
    cause = _ROOT_CAUSE[fmt]
    try:
        if fmt == "json":
            data = json.loads(text)
        elif fmt == "toml":
            import tomllib

            data = tomllib.loads(text)
        elif fmt == "yaml":
            yaml = _yaml()
            try:
                # safe_load, never load: a config file is untrusted input and
                # the full loader constructs arbitrary Python objects from
                # tags, which is a code-execution primitive pointed at a file
                # this tool did not write.
                data = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ConfigError(f"{path} is not valid YAML ({exc}); refusing to edit it") from exc
        else:
            raise ConfigError(f"unknown config format {fmt!r} (expected one of {', '.join(FORMATS)})")
    except ConfigError:
        raise
    except Exception as exc:  # tomllib.TOMLDecodeError, json.JSONDecodeError
        raise ConfigError(f"{path} is not valid {cause} ({exc}); refusing to edit it") from exc
    # An empty YAML file parses to None, and an editor writes exactly that.
    # Only the blank case is folded into an empty mapping: a document that
    # really holds a list, a number or a string is a shape this tool must not
    # guess its way through.
    if data is None:
        if not text.strip():
            return {}
        raise ConfigError(f"{path} is empty; refusing to edit it")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} does not contain {_ROOT_LABEL[fmt]}; refusing to edit it")
    return data


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #


def _has_comment_line(text: str) -> bool:
    """Whether any line begins with a comment marker.

    Only a line whose first non-blank character is the marker counts. A ``#``
    inside a quoted string is not a comment, and a heuristic that tried to tell
    those apart would misfire on exactly the files it is meant to protect.
    """
    return any(line.lstrip().startswith("#") for line in text.splitlines())


def _toml_null_path(value: Any, trail: str = "") -> str | None:
    """The dotted path to the first ``None`` in ``value``, if there is one.

    TOML has no null. A document holding one cannot be written back without
    silently dropping or inventing a key, so it is refused by path instead.
    """
    if value is None:
        return trail or "<root>"
    if isinstance(value, dict):
        for key, item in value.items():
            found = _toml_null_path(item, f"{trail}.{key}" if trail else str(key))
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _toml_null_path(item, f"{trail}[{index}]")
            if found:
                return found
    return None


def render_document(fmt: str, data: dict[str, Any]) -> str:
    """Serialise ``data`` in ``fmt``, or raise :class:`ConfigError`.

    The round-trip guarantee is ``_parse(fmt, render_document(fmt, data)) ==
    data`` for every mapping this module is willing to write, and it is what
    the per-format tests pin. Formatting is not stable across a re-read, and
    that is stated rather than pretended away: the promise is about the
    structure a host reads, not the bytes a human typed.
    """
    if fmt == "json":
        return json.dumps(data, indent=2) + "\n"
    if fmt == "toml":
        null_at = _toml_null_path(data)
        if null_at:
            raise ConfigError(
                f"{null_at} is null, and TOML has no null to write it as; "
                "removing the key first would change what the host reads"
            )
        dumped: str = _toml_writer().dumps(data)
        return dumped
    if fmt == "yaml":
        yaml = _yaml()
        # sort_keys=False keeps the operator's key order, which is the one part
        # of a YAML file a reader actually navigates by.
        rendered = yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            # Long enough that no scalar is wrapped: a wrapped string still
            # round-trips, but it turns one readable line into several.
            width=4096,
        )
        return str(rendered)
    raise ConfigError(f"unknown config format {fmt!r} (expected one of {', '.join(FORMATS)})")


def write_document(fmt: str, path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` in ``fmt``, refusing a file it cannot preserve.

    A whole-document re-serialisation is the only way to write these formats
    from standard-library code, so it is only safe on a file this module can
    reproduce. JSON round-trips exactly, which is why the JSON hosts never
    hit this. TOML and YAML do not: comments are not in the parsed document at
    all, so writing one back loses them silently. Rather than trade a user's
    annotated config for a clean file, an existing file carrying comments is
    reported and left alone.
    """
    if fmt in ("toml", "yaml") and path.exists():
        comments = _has_comment_line(path.read_text(encoding="utf-8"))
        if comments:
            raise ConfigError(
                f"{path} contains comments, which a {fmt.upper()} writer cannot preserve. "
                "Registering this server would discard them, so nothing was written. "
                "Add the entry by hand, or strip the comments and re-run."
            )
    text = render_document(fmt, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

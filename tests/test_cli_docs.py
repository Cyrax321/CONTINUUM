"""Every CLI subcommand must have a row in the reference table (#632).

The table in docs/api/cli.md drifted behind the parser twice (#360, #632).
Rows carry usage syntax after the name, so the guard anchors on Command-column
row starts instead of bare substrings: prose mentions (`` `resume --json` ``)
and prefix collisions (`` `attest `` matching `` `attest-keygen` ``) must not
mask a deleted row.
"""

from __future__ import annotations

import re
from pathlib import Path

from continuum.cli.main import build_parser

TABLE = Path(__file__).resolve().parents[1] / "docs" / "api" / "cli.md"


def _row_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"^\|\s*`" + re.escape(name) + r"(?=[\s`|])", re.MULTILINE)


def test_every_subcommand_has_a_table_row() -> None:
    table = TABLE.read_text(encoding="utf-8")
    subs = sorted(build_parser()._subparsers._group_actions[0].choices.keys())
    assert subs, "parser exposes no subcommands"
    missing = [name for name in subs if not _row_pattern(name).search(table)]
    assert not missing, f"subcommands without a docs/api/cli.md row: {missing}"


def _hooks_section() -> str:
    """The ## hooks section of the CLI reference, where profiles live."""
    text = TABLE.read_text(encoding="utf-8")
    start = text.index("## hooks")
    rest = text[start + len("## hooks") :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_hooks_section_names_every_client_profile() -> None:
    """Pinned prose over CLIENT_PROFILES must not drift silently (#777)."""
    from continuum.clienthooks import CLIENT_PROFILES

    section = _hooks_section()
    assert CLIENT_PROFILES, "no client profiles to guard"
    missing: list[str] = []
    for profile, fields in CLIENT_PROFILES.items():
        if profile not in section:
            missing.append(profile)
        for key, value in fields.items():
            if not isinstance(value, str) or not value:
                missing.append(f"{profile}.{key}")
            elif value not in section:
                missing.append(f"{profile}.{key}={value}")
    assert not missing, f"hooks section omits CLIENT_PROFILES entries: {missing}"

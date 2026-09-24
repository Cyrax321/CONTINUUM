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
REF_CLI = Path(__file__).resolve().parents[1] / "references" / "cli.md"
LANDING = Path(__file__).resolve().parents[1] / "docs" / "index.html"


def _row_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"^\|\s*`" + re.escape(name) + r"(?=[\s`|])", re.MULTILINE)


def test_every_subcommand_has_a_table_row() -> None:
    table = TABLE.read_text(encoding="utf-8")
    subs = sorted(build_parser()._subparsers._group_actions[0].choices.keys())
    assert subs, "parser exposes no subcommands"
    missing = [name for name in subs if not _row_pattern(name).search(table)]
    assert not missing, f"subcommands without a docs/api/cli.md row: {missing}"


def test_references_cli_names_every_subcommand() -> None:
    """Every CLI subcommand must appear in references/cli.md (#795)."""
    text = REF_CLI.read_text(encoding="utf-8")
    subs = sorted(build_parser()._subparsers._group_actions[0].choices.keys())
    assert subs, "parser exposes no subcommands"
    missing = [
        name
        for name in subs
        if not re.search(r"^continuum " + re.escape(name) + r"\b", text, re.MULTILINE)
    ]
    assert not missing, f"subcommands omitted from references/cli.md: {missing}"


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


def test_readme_module_map_command_count_matches_parser() -> None:
    """The README module map `cli/` row must state the live command count (#754).

    The row read 38 while the parser built 44. The figure rots with every
    new subcommand, so the guard compares the documented number against the
    parser instead of pinning a literal.
    """
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^\|\s*`cli/`\s*\|\s*(\d+) argparse commands", readme, re.MULTILINE)
    assert match, "README module map has no `cli/` argparse-commands count to guard"
    documented = int(match.group(1))
    live = len(build_parser()._subparsers._group_actions[0].choices)
    assert documented == live, (
        f"README module map says {documented} argparse commands but the parser builds {live}"
    )


def test_landing_page_command_count_matches_parser() -> None:
    """The landing page's CLI-command figure must match the parser (#1283).

    ``docs/index.html`` is the first thing a visitor sees and it stated 45
    while the parser built 46. The page states the count twice -- the meta
    description search engines read and the metrics card -- and a figure that
    disagrees with either the parser or itself is wrong, so both are compared
    against the parser rather than pinned.
    """
    html = LANDING.read_text(encoding="utf-8")
    live = len(build_parser()._subparsers._group_actions[0].choices)
    documented = set()
    for pattern in (
        re.compile(r"([\d,]+)\s+CLI commands"),
        re.compile(
            r'class="metric-value">([\d,]+)</span>\s*<span class="metric-label">CLI COMMANDS'
        ),
    ):
        match = pattern.search(html)
        assert match, f"docs/index.html no longer states a CLI-command count ({pattern.pattern})"
        documented.add(int(match.group(1).replace(",", "")))
    assert len(documented) == 1, (
        f"docs/index.html states inconsistent CLI-command counts: {sorted(documented)}"
    )
    assert documented == {live}, (
        f"docs/index.html says {documented.pop()} CLI commands but the parser builds {live}"
    )


def test_references_cli_documented_flag_defaults_match_parser() -> None:
    """A documented `[--flag N]` default must match the parser's (#1102).

    ``references/cli.md`` advertised the dashboard on port 8080 while the
    parser and ``serve_dashboard`` both default to 8000. The reference is a
    flat list of usage rows, so the guard reads the default each row prints
    and compares it with the parser instead of pinning a literal, which also
    covers the ``serve --port`` row (8765).
    """
    text = REF_CLI.read_text(encoding="utf-8")
    subparsers = build_parser()._subparsers._group_actions[0].choices
    defaults: dict[str, dict[str, int]] = {}
    for name in ("dashboard", "serve"):
        per_command: dict[str, int] = {}
        for action in subparsers[name]._actions:
            for flag in action.option_strings:
                if isinstance(action.default, int):
                    per_command[flag] = action.default
        defaults[name] = per_command

    checked = False
    for line in text.splitlines():
        match = re.match(r"^continuum (dashboard|serve)(.*)$", line)
        if not match:
            continue
        command = match.group(1)
        for flag, documented in re.findall(r"\[(--\S+)\s+(\d+)\]", match.group(2)):
            checked = True
            assert flag in defaults[command], f"{command} documents unknown flag {flag}"
            live = defaults[command][flag]
            assert int(documented) == live, (
                f"{match.group(1)} {flag} documents {documented} but the parser defaults to {live}"
            )
    assert checked, "no [--flag N] default reached the guard; the regex drifted"

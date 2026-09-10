"""The documented MCP surface must match the one the server actually serves.

``docs/api/mcp.md`` is what a client author reads before writing any code
against this server, so a tool registered without a row there is a tool nobody
knows to call. ``continuum_record_plan`` shipped that way and the table stayed
at eleven rows (issue #271), which is why the audit is a test rather than a
periodic reread: the drift is silent, and ``tools/list`` is the only authority
on it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from continuum.mcp.server import build_server
from continuum.storage import SQLiteStorage

#: The API reference page whose tool table mirrors ``tools/list``.
MCP_DOC = Path(__file__).resolve().parents[1] / "docs" / "api" / "mcp.md"

#: The MCP adversarial audit report whose coverage table must also stay complete.
MCP_AUDIT = Path(__file__).resolve().parents[1] / "docs" / "TESTING_MCP.md"

#: One documented tool: name and kind. The purpose column is prose and is left
#: to a human reviewer, as is the sentence of totals under the table; the two
#: columns that can silently contradict the server are not.
ROW = re.compile(r"^\| `(continuum_\w+)` \| (mutate|read) \|", re.MULTILINE)

#: Tool names listed in the audit coverage table (issue #759).
AUDIT_TOOL = re.compile(r"^\| `(continuum_\w+)` \|", re.MULTILINE)

#: The character house style bans, by code point so this file carries none.
EM_DASH = chr(0x2014)

#: Files whose shell commands document how to install the package, so the
#: extras are spelled and quoted the same way in each of them.
ROOT = Path(__file__).resolve().parents[1]
INSTALL_DOCS = [
    ROOT / "docs" / "api" / "README.md",
    ROOT / "docs" / "api" / "mcp.md",
    ROOT / "README.md",
    ROOT / "references" / "install.md",
    ROOT / "references" / "adapters.md",
    ROOT / "references" / "quickstart.md",
]

#: An install command and its first non-flag argument: the target an extra
#: appears in, if one appears at all.
INSTALL_COMMAND = re.compile(r"(?:pip|uv pip) install\s+(?:-\S+\s+)*(\S+)")


def _extras_targets(text: str) -> list[str]:
    """Every install target in ``text`` that carries an extras bracket."""
    return [target for target in INSTALL_COMMAND.findall(text) if "[" in target]


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    yield storage
    storage.close()


@pytest.fixture
def server(store: SQLiteStorage) -> Any:
    # No policy: listing the tools is read-only, and the deny-by-default
    # allowlist is what test_mcp_authz.py covers.
    srv, _ = build_server(storage=store)
    return srv


def kinds(tools: list[Any]) -> dict[str, str]:
    """Map each served tool to the kind the table spells in its Kind column."""
    return {
        tool.name: "read" if tool.annotations and tool.annotations.read_only_hint else "mutate"
        for tool in tools
    }


@pytest.mark.asyncio
async def test_the_table_lists_every_served_tool_with_its_kind(server: Any) -> None:
    """A row per tool, and the Kind column reads off the declared annotation.

    Failing here means the table and ``tools/list`` disagree; the sentence of
    totals directly under the table is part of the same edit. Duplicate rows
    are rejected before the comparison, or a second row for one tool would
    silently decide its kind.
    """
    rows = ROW.findall(MCP_DOC.read_text(encoding="utf-8"))
    assert len(rows) == len({name for name, _ in rows}), "a tool is documented twice"
    assert dict(rows) == kinds(await server.list_tools())


@pytest.mark.asyncio
async def test_the_audit_coverage_table_lists_every_served_tool(server: Any) -> None:
    """``docs/TESTING_MCP.md`` must name every tool ``tools/list`` exposes.

    The audit once claimed "all 11 tools" while ``continuum_record_plan`` was
    already served (issue #759). Pinning the coverage table to ``tools/list``
    keeps that claim from rotting again.
    """
    text = MCP_AUDIT.read_text(encoding="utf-8")
    section = text.split("## Tool coverage", 1)[1].split("## Findings", 1)[0]
    covered = AUDIT_TOOL.findall(section)
    assert len(covered) == len(set(covered)), "a tool is audited twice"
    served = {tool.name for tool in await server.list_tools()}
    assert set(covered) == served
    assert "All 12 tools were exercised" in text


def test_the_page_carries_no_em_dashes() -> None:
    """House style forbids them (issue #266) and one had reached the table."""
    assert EM_DASH not in MCP_DOC.read_text(encoding="utf-8")


def test_documented_extras_name_the_real_package() -> None:
    """No install command teaches ``continuum[...]``, the wrong package.

    A real PyPI package exists under the bare name ``continuum``, so a doc
    that sends the operator to it installs something unrelated instead of the
    extra (issue #836, the bug the remediation message shipped with until
    #719). Removing every legitimate ``continuum-agent[`` first leaves only
    the wrong spellings behind.
    """
    for path in INSTALL_DOCS:
        text = path.read_text(encoding="utf-8")
        assert "continuum[" not in text.replace("continuum-agent[", ""), (
            f"{path} teaches an install of the wrong package"
        )


def test_documented_extras_are_quoted() -> None:
    """Every extras-bearing install target is quoted.

    Unquoted brackets are a glob in zsh, where ``pip install continuum-agent[
    mcp]`` either fails or silently expands to the files that happen to match
    (issue #836). Quoting is the documented house form in every context, so
    the guard scans the target of every install command that carries one.
    """
    for path in INSTALL_DOCS:
        for target in _extras_targets(path.read_text(encoding="utf-8")):
            assert target.startswith('"'), f"{path}: unquoted extras in {target}"

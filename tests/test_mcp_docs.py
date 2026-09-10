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


#: The repo root, for scanning the docs that state the MCP tool count.
ROOT = Path(__file__).resolve().parents[1]

#: Prose and tables that state how many tools the MCP server serves: the
#: markdown docs, the marketing page, and the architecture drawing's text.
TOOL_COUNT_DOCS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    *sorted((ROOT / "docs").rglob("*.html")),
    *sorted((ROOT / "docs").rglob("*.svg")),
    *sorted((ROOT / "references").rglob("*.md")),
]

#: A tool-count claim: "12 tools", "eleven tools", "12 stdio tools",
#: "twelve MCP tools", "the twelve-tool server". The number may be digits or
#: a word up to thirteen, and up to one adjective may sit before "tools".
_TOOL_COUNT_CLAIM = re.compile(
    r"\b(\d+|eleven|twelve|thirteen)[-\s]+(?:stdio\s+|MCP\s+)?tools\b", re.IGNORECASE
)
_WORD_TO_NUM = {"eleven": 11, "twelve": 12, "thirteen": 13}

#: The mutating half of the same claim: "9 mutating", "8 mutating".
_MUTATING_CLAIM = re.compile(r"\b(\d+)\s+mutating\b", re.IGNORECASE)

#: A claim only counts when it is about this server, not a surveyed external
#: system (the research notes describe benchmarks with their own tool counts),
#: so the window around the claim must name the server or its transport.
_ABOUT_US = re.compile(r"mcp|continuum|stdio|inspector", re.IGNORECASE)


def _claims_about_us(text: str, match: re.Match[str]) -> bool:
    window = text[max(0, match.start() - 200) : match.end() + 200]
    return bool(_ABOUT_US.search(window))


@pytest.mark.asyncio
async def test_documented_tool_counts_match_the_server(server: Any) -> None:
    """Every tool-count claim in the docs equals what ``tools/list`` serves.

    The count drifted twice (#271, #759): tools were added and the prose kept
    saying eleven, in ten files at once (#840). The server is the only
    authority, so every claim about it is compared against it, digits and
    words alike, including the read-only/mutating split. Claims about other
    systems' tools (the research surveys) are left alone.
    """
    tools = await server.list_tools()
    served = len(tools)
    mutating = sum(
        1 for tool in tools if not (tool.annotations and tool.annotations.read_only_hint)
    )
    for path in TOOL_COUNT_DOCS:
        text = path.read_text(encoding="utf-8")
        for match in _TOOL_COUNT_CLAIM.finditer(text):
            if not _claims_about_us(text, match):
                continue
            claim = _WORD_TO_NUM.get(match.group(1).lower())
            if claim is None:
                claim = int(match.group(1))
            assert claim == served, (
                f"{path} claims {match.group(0)!r} but tools/list serves {served}"
            )
        for match in _MUTATING_CLAIM.finditer(text):
            if not _claims_about_us(text, match):
                continue
            assert int(match.group(1)) == mutating, (
                f"{path} claims {match.group(0)!r} but {mutating} tools mutate"
            )

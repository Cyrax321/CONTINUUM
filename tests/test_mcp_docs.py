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

#: One documented tool: name and kind. The purpose column is prose and is left
#: to a human reviewer, as is the sentence of totals under the table; the two
#: columns that can silently contradict the server are not.
ROW = re.compile(r"^\| `(continuum_\w+)` \| (mutate|read) \|", re.MULTILINE)

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


def test_the_page_carries_no_em_dashes() -> None:
    """House style forbids them (issue #266) and one had reached the table."""
    assert EM_DASH not in MCP_DOC.read_text(encoding="utf-8")

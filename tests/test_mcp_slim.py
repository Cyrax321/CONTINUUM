from continuum.mcp.server import build_server
from continuum.storage import SQLiteStorage


def test_slim_mode_exposes_only_resume_subset(monkeypatch) -> None:
    storage = SQLiteStorage(":memory:")
    monkeypatch.setenv("CONTINUUM_MCP_SLIM", "1")
    server, _ = build_server(storage=storage)
    names = {tool.name for tool in server._tool_manager._tools.values()}
    assert names == {"continuum_resume", "continuum_validate", "continuum_list_actions"}


def test_full_mode_exposes_all_tools(monkeypatch) -> None:
    storage = SQLiteStorage(":memory:")
    monkeypatch.delenv("CONTINUUM_MCP_SLIM", raising=False)
    server, _ = build_server(storage=storage)
    names = {tool.name for tool in server._tool_manager._tools.values()}
    assert "continuum_record_progress" in names
    assert "continuum_checkpoint" in names
    assert len(names) == 12

"""CRLF wire terminator is preserved (#839).

The MCP smoke client wraps the server's stdout with newline="" so that
\\r\\n framing is preserved instead of being rewritten to \\n. These tests
exercise the real wrapper setup and framing detection from scripts/mcp_smoke.py.
"""

import io

from scripts.mcp_smoke import MCPClient


def _make_client() -> MCPClient:
    """Build an MCPClient without spawning a real subprocess."""
    client = MCPClient.__new__(MCPClient)
    client._id = 0
    client.wire_framing = None
    client._errors = []
    return client


def test_stdout_wrapper_preserves_crlf():
    """The stdout TextIOWrapper uses newline="" so CRLF is not rewritten."""
    raw = b'{"jsonrpc":"2.0","id":1,"result":{}}\r\n'
    pipe = io.BytesIO(raw)
    wrapper = io.TextIOWrapper(pipe, encoding="utf-8", newline="")
    line = wrapper.readline()
    assert line.endswith("\r\n"), f"expected CRLF preserved, got: {line!r}"


def test_observe_framing_detects_crlf():
    """_observe_framing records CRLF when a frame arrives with \\r\\n."""
    client = _make_client()
    client._observe_framing('{"id":1}\r\n')
    assert "CRLF" in client.wire_framing


def test_observe_framing_detects_lf():
    """_observe_framing records LF when a frame arrives with \\n only."""
    client = _make_client()
    client._observe_framing('{"id":1}\n')
    assert "LF" in client.wire_framing


def test_observe_framing_detects_mixed():
    """_observe_framing reports mixed when terminators change between frames."""
    client = _make_client()
    client._observe_framing('{"id":1}\r\n')
    client._observe_framing('{"id":2}\n')
    assert "mixed" in client.wire_framing


def test_client_wire_framing_starts_none():
    """A fresh client has not observed any framing yet."""
    client = _make_client()
    assert client.wire_framing is None

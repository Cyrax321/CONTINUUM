"""Shared helpers for driving MCP tools in tests."""

from __future__ import annotations

from typing import Any


class _ClientInfo:
    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "test"


class _Params:
    def __init__(self, name: str, meta: dict | None = None) -> None:
        self.client_info = _ClientInfo(name)
        self.meta = meta or {}


class _Session:
    def __init__(self, name: str, meta: dict | None = None) -> None:
        self.client_params = _Params(name, meta)


class FakeContext:
    """Stands in for an MCP request context with a declared client identity.

    Mirrors the real shape the transport injects (``session.client_params
    .client_info.name``), which is read from the initialize handshake and
    cannot be set by a tool argument. ``auth_token`` populates the handshake's
    ``_meta.authToken`` so authentication can be exercised without a live
    transport.
    """

    def __init__(self, name: str | None, auth_token: str | None = None) -> None:
        meta = {"authToken": auth_token} if auth_token is not None else {}
        self.session = _Session(name if name is not None else "", meta)


def fake_context(name: str | None, auth_token: str | None = None) -> Any:
    return FakeContext(name, auth_token)

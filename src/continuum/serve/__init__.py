"""Python client and entry point for the CONTINUUM sidecar.

The wire protocol is language-agnostic (newline-delimited JSON). This client
is the reference implementation; the same bytes work from any language. For
local use, :func:`serve_subprocess` launches ``continuum serve`` and returns a
connected client.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from typing import IO, Any, TextIO, cast

from continuum.serve.server import (
    BadParams,
    BadRequest,
    MalformedRunLog,
    MethodNotFound,
    NotAuthorized,
    SidecarAuth,
    SidecarError,
    SidecarServer,
    list_methods,
)

__all__ = [
    "SidecarServer",
    "SidecarAuth",
    "SidecarClient",
    "SidecarClientError",
    "serve_subprocess",
    "run_serve",
    "cmd_serve",
    "list_methods",
    "SidecarError",
    "MethodNotFound",
    "NotAuthorized",
    "BadParams",
    "BadRequest",
    "MalformedRunLog",
]


class SidecarClientError(Exception):
    """Raised when the sidecar returns an error response."""


class SidecarClient:
    """Talks the sidecar wire protocol over a pair of text streams."""

    def __init__(self, instream: IO[str], outstream: IO[str]) -> None:
        self._in = instream
        self._out = outstream
        self._next_id = 0

    def request(self, method: str, **params: Any) -> dict[str, Any]:
        rid = self._next_id
        self._next_id += 1
        self._out.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        self._out.flush()
        while True:
            line = self._in.readline()
            if not line:
                raise SidecarClientError("sidecar closed the connection")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                err = msg["error"]
                raise SidecarClientError(f"{err.get('type', 'error')}: {err.get('message', '')}")
            return cast("dict[str, Any]", msg["result"])

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._out.close()


class SubprocessClient:
    """A :class:`SidecarClient` backed by a ``continuum serve`` process."""

    def __init__(self, process: subprocess.Popen[str], client: SidecarClient) -> None:
        self._process = process
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def terminate(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()


def serve_subprocess(
    db: str = "continuum.db", *, token: str | None = None, executable: str | None = None
) -> SubprocessClient:
    """Launch ``continuum serve`` and return a connected client."""
    cmd = [executable or sys.executable, "-m", "continuum.cli.main", "--db", db, "serve"]
    env = dict(os.environ)
    if token is not None:
        env["CONTINUUM_SERVE_TOKEN"] = token
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    client = SidecarClient(proc.stdout, proc.stdin)
    return SubprocessClient(proc, client)


def run_serve(
    db: str | None = None,
    transport: str = "stdio",
    instream: TextIO | None = None,
    outstream: TextIO | None = None,
    port: int = 8765,
) -> int:
    """Run the sidecar loop. Defaults to stdin/stdout for a real server."""
    server = SidecarServer(database=db)
    if transport == "http":
        from continuum.serve.server import SidecarHTTP

        http = SidecarHTTP(server, port=port)
        print(f"CONTINUUM sidecar listening on http://{'127.0.0.1'}:{http.port}", flush=True)
        try:
            http.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            http.shutdown()
            server.close()
        return 0
    try:
        return server.serve_stdio(instream, outstream)
    finally:
        server.close()


def cmd_serve(args: Any, storage: Any, out: TextIO, err: TextIO) -> int:  # noqa: ANN401
    """CLI entry point for ``continuum serve``."""
    from continuum.storage import Storage

    if storage is not None and isinstance(storage, Storage):
        storage.close()
    return run_serve(
        db=getattr(args, "db", None),
        transport=getattr(args, "transport", "stdio"),
        port=int(getattr(args, "port", 8765)),
    )

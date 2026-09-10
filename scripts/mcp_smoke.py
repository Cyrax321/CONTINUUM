#!/usr/bin/env python3
"""Drive the CONTINUUM MCP server live over stdio and print the protocol traffic.

    python scripts/mcp_smoke.py
    python scripts/mcp_smoke.py --entry-point module

Starts the real server as a subprocess and speaks raw JSON-RPC to it: no test
harness, no in-process shortcut. Every frame sent and received is printed as it
happens, so what you see is the wire, not a summary of it.

By default the smoke test spawns the installed ``continuum-mcp`` console
script, because PATH resolution of that entry point is the single most common
way a real installation fails while every module-form test still passes
(issue #839). When the script is not on PATH it falls back to
``python -m continuum.mcp`` against the worktree's ``src`` and says so; pass
``--entry-point console-script`` to refuse the fallback, or
``--entry-point module`` to force it.

The transcript also reports the observed wire framing of the response frames
(``\\n`` or ``\\r\\n``): on Windows the MCP SDK terminates stdio frames with
CRLF (modelcontextprotocol/python-sdk#2433), which lenient clients absorb and
strict NDJSON clients reject, so the smoke output states which one this server
actually produced.

The point of the transcript is step 7. The same action is intercepted twice with
identical arguments; the second call must answer ``proceed: false`` and hand back
the first result. That is the difference between an agent that retries a side
effect after a restart and one that does not.

Standalone and read-only with respect to the repository: it writes to a
throwaway database in the platform temporary directory and touches nothing
else.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

# The transcript below is drawn with box-drawing characters and em dashes, so
# stdout has to be able to encode them. Python picks the locale encoding for a
# redirected stream -- cp1252 on Windows, ascii under LANG=C -- and the script
# died with a UnicodeEncodeError at the first banner as soon as its output was
# piped anywhere, which is what CI and any log capture do. Interactive consoles
# already speak UTF-8, so this only changes the redirected case.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")

# tempfile rather than a literal /tmp: the latter does not exist on Windows,
# where it left the script unable to open its own database and dying at the
# initialize handshake.
DB_PATH = str(Path(tempfile.gettempdir()) / "mcp-smoke-demo.db")
PROTOCOL_VERSION = "2024-11-05"

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _COLOUR else text


def banner(step: str, title: str) -> None:
    print()
    print(paint(f"{'─' * 74}", DIM))
    print(paint(f"  {step}  {title}", BOLD + CYAN))
    print(paint(f"{'─' * 74}", DIM))


def note(text: str, colour: str = YELLOW) -> None:
    print(paint(f"    {text}", colour), flush=True)


#: The console script a real MCP host spawns; also the name ``.mcp.json``
#: declares, resolved by the host through PATH.
CONSOLE_SCRIPT = "continuum-mcp"


def _resolve_command(choice: str) -> tuple[list[str], bool]:
    """Return the argv to spawn and whether the module fallback was used.

    The console script is resolved through PATH because PATH resolution is
    the failure mode this smoke test exists to cover (#839): a bare command
    name in ``.mcp.json`` is looked up by the host, so the smoke test must
    look it up the same way instead of bypassing it with ``python -m``.
    """
    if choice == "module":
        return [sys.executable, "-u", "-m", "continuum.mcp"], False
    resolved = shutil.which(CONSOLE_SCRIPT)
    if resolved:
        return [resolved], False
    if choice == "console-script":
        raise SystemExit(
            f"error: {CONSOLE_SCRIPT} is not on PATH. Install it (pip install "
            '"continuum-agent[mcp]") or pass --entry-point module to smoke-test '
            "the worktree source instead."
        )
    return [sys.executable, "-u", "-m", "continuum.mcp"], True


class MCPClient:
    """A minimal JSON-RPC client over the server's stdin/stdout."""

    def __init__(self, db_path: str, entry_point: str = "auto") -> None:
        cmd, self.fell_back = _resolve_command(entry_point)
        env = dict(os.environ)
        env["CONTINUUM_DB"] = db_path
        # Mutating tools deny unlisted callers by default, so the demo grants
        # itself access the same way a real deployment would. Without this the
        # server is read-only and step 2 onward is refused, which is the
        # intended posture for an unconfigured server, not a bug.
        env["CONTINUUM_MCP_ALLOW"] = "mcp-smoke"
        # The PYTHONPATH injection belongs to the module form only: against
        # the worktree's src it is how a source-tree run works, but pointed at
        # the console script it would shadow the very install under test.
        if "-m" in cmd:
            src = Path(__file__).resolve().parents[1] / "src"
            if src.is_dir():
                existing = env.get("PYTHONPATH")
                env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else str(src)

        # Binary pipes wrapped by hand: Popen's text=True would enable
        # universal newlines and silently rewrite \r\n to \n, hiding the wire
        # framing this script reports (#839). newline="" preserves what the
        # server actually wrote; newline="\n" keeps what we send as LF.
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._stdin = io.TextIOWrapper(self.proc.stdin, encoding="utf-8", newline="\n")
        self._stdout = io.TextIOWrapper(self.proc.stdout, encoding="utf-8", newline="")
        self._id = 0
        #: The terminator observed on response frames so far: "CRLF (\r\n)",
        #: "LF (\n)", or None before the first frame arrives.
        self.wire_framing: str | None = None
        # Drain stderr on a thread so a chatty server cannot deadlock us by
        # filling the pipe buffer while we block reading stdout.
        self._errors: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._errors.append(raw.decode("utf-8", errors="replace").rstrip())

    def _send(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload)
        print(paint("  --> ", GREEN) + paint(raw, DIM), flush=True)
        self._stdin.write(raw + "\n")
        self._stdin.flush()

    def _read(self) -> dict[str, Any]:
        line = self._stdout.readline()
        if not line:
            errors = "\n".join(self._errors[-20:])
            raise RuntimeError(f"server closed the connection.\nstderr:\n{errors}")
        self._observe_framing(line)
        print(paint("  <-- ", CYAN) + paint(line.rstrip(), DIM), flush=True)
        return json.loads(line)

    def _observe_framing(self, line: str) -> None:
        ending = "CRLF (\\r\\n)" if line.endswith("\r\n") else "LF (\\n)"
        if self.wire_framing is None:
            self.wire_framing = ending
        elif ending not in self.wire_framing:
            print(paint(f"    note: frame terminator changed to {ending}", YELLOW), flush=True)
            self.wire_framing = f"mixed, last seen {ending}"

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        return self._read()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool and return its parsed JSON payload."""
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response.get("result", {})
        content = result.get("content") or []
        if not content:
            return {"_error": response.get("error"), "_raw": result}
        return json.loads(content[0]["text"])

    def close(self) -> None:
        self._stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def show(payload: dict[str, Any], *, indent: str = "    ") -> None:
    """Pretty-print a decoded tool payload beneath the raw frame."""
    text = json.dumps(payload, indent=2, sort_keys=True)
    for line in text.split("\n"):
        print(paint(f"{indent}{line}", ""), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive the CONTINUUM MCP server live over stdio and print the protocol traffic."
    )
    parser.add_argument(
        "--entry-point",
        choices=("auto", "console-script", "module"),
        default="auto",
        help=(
            "how to spawn the server: the installed console script (auto uses it when "
            "on PATH), or python -m against the worktree source"
        ),
    )
    args = parser.parse_args()

    if Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
    for suffix in ("-wal", "-shm"):
        stale = Path(DB_PATH + suffix)
        if stale.exists():
            stale.unlink()

    print(paint("CONTINUUM MCP: live stdio smoke test", BOLD))
    print(paint(f"database: {DB_PATH} (fresh)", DIM))
    client = MCPClient(DB_PATH, entry_point=args.entry_point)
    print(paint(f"server:   {' '.join(client.proc.args)}", DIM))
    if client.fell_back:
        print(
            paint(
                f"note:     {CONSOLE_SCRIPT} is not on PATH, so the module form runs against "
                "the worktree source. This covers the code, not the installation.",
                YELLOW,
            )
        )
    run_id = "run_demo_1"
    action_args = {"to": "team@example.com"}
    failures: list[str] = []

    try:
        # 1 -- handshake ------------------------------------------------- #
        banner("1", "initialize handshake")
        response = client.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-smoke", "version": "1.0"},
            },
        )
        server_info = response.get("result", {}).get("serverInfo", {})
        note(f"connected to {server_info.get('name')} ({server_info.get('title')})", GREEN)
        note(f"response frames end with {client.wire_framing}", GREEN)
        client.notify("notifications/initialized")

        banner("1b", "tools/list")
        listed = client.request("tools/list")
        tools = listed.get("result", {}).get("tools", [])
        note(f"{len(tools)} tools available", GREEN)
        for tool in tools:
            read_only = (tool.get("annotations") or {}).get("readOnlyHint")
            marker = "read-only" if read_only else "mutates  "
            print(paint(f"      [{marker}] {tool['name']}", DIM), flush=True)

        # 2 -- checkpoint a fresh run ------------------------------------ #
        banner("2", "continuum_checkpoint: fresh run")
        payload = client.call_tool(
            "continuum_record_progress",
            {"run_id": run_id, "completed": 0, "total": 10, "goal": "Demo the MCP server live"},
        )
        show(payload)
        payload = client.call_tool(
            "continuum_checkpoint", {"run_id": run_id, "reason": "initial checkpoint"}
        )
        show(payload)
        note(f"checkpoint v{payload.get('version')} sealed", GREEN)

        # 3 -- record progress ------------------------------------------- #
        banner("3", "continuum_record_progress: 1..5 of 10")
        for completed in range(1, 6):
            payload = client.call_tool(
                "continuum_record_progress",
                {"run_id": run_id, "completed": completed, "total": 10},
            )
            show(payload)

        # 4 -- intercept an external action ------------------------------ #
        banner("4", "continuum_intercept_action: first attempt")
        first = client.call_tool(
            "continuum_intercept_action",
            {"run_id": run_id, "action_type": "send_email", "arguments": action_args},
        )
        show(first)
        if first.get("proceed") is True:
            note("proceed=true: nothing has claimed this action yet", GREEN)
        else:
            failures.append("first interception should have granted proceed=true")
            note("proceed was not true", RED)

        action_key = first.get("action_key", "")

        # 5 -- the agent performs the effect, then reports back ---------- #
        banner("5", "the caller performs the side effect")
        note("(pretend) SMTP: email delivered to team@example.com, id msg_7781", "")
        banner("5b", "continuum_complete_action")
        payload = client.call_tool(
            "continuum_complete_action",
            {
                "run_id": run_id,
                "action_key": action_key,
                "external_id": "msg_7781",
                "result": {"delivered": True, "recipient": "team@example.com"},
            },
        )
        show(payload)

        # 6 -- the same action again ------------------------------------- #
        banner("6", "continuum_intercept_action: SAME action, again")
        note("This is the whole point of the ledger.", "")
        second = client.call_tool(
            "continuum_intercept_action",
            {"run_id": run_id, "action_type": "send_email", "arguments": action_args},
        )
        show(second)

        if second.get("proceed") is False and second.get("external_id") == "msg_7781":
            note("proceed=FALSE: the email is NOT sent twice", GREEN)
            note(f"previous result returned instead: {second.get('previous_result')}", GREEN)
        else:
            failures.append("duplicate interception was not refused")
            note("DUPLICATE NOT PREVENTED", RED)

        # 7 -- validate and resume ---------------------------------------- #
        banner("7", "continuum_validate")
        payload = client.call_tool("continuum_validate", {"run_id": run_id})
        show(payload)

        banner("8", "continuum_resume")
        decision = client.call_tool("continuum_resume", {"run_id": run_id})
        show({k: v for k, v in decision.items() if k not in ("report", "contract_text")})
        print()
        print(paint("    ── rendered report ──", DIM))
        for line in str(decision.get("report", "")).split("\n"):
            print(paint(f"    {line}", ""), flush=True)

        # Expectation changed when event provenance landed. Everything written
        # through MCP is tagged EXTERNAL_AGENT (an agent's unverified report
        # about its own work) so the run is deliberately NOT certified as
        # resumable on that basis alone. It previously returned mode=resume,
        # which meant an agent could fabricate progress and have CONTINUUM
        # confirm it was safe to continue. Requiring review is the fix, not a
        # regression. The ledger is separately clean: the side effect really
        # was performed exactly once, and that is what step 6 proves.
        mode = decision.get("mode")
        uncertain = decision.get("uncertain_actions") or []
        if mode != "resume" and not uncertain:
            note(f"mode={mode}: agent-reported state requires review (expected)", GREEN)
            note("no uncertain side effects: the ledger itself is clean", GREEN)
        elif uncertain:
            failures.append(f"unexpected uncertain actions: {uncertain}")
            note(f"uncertain actions outstanding: {uncertain}", RED)
        else:
            failures.append("agent self-reported state was certified as resumable")
            note(f"mode={mode}: self-reported state should not be 'resume'", RED)

        banner("9", "ledger contents")
        show(client.call_tool("continuum_list_actions", {"run_id": run_id}))

        # summary --------------------------------------------------------- #
        print()
        print(paint("─" * 74, DIM))
        if failures:
            print(paint("  SMOKE TEST FAILED", BOLD + RED))
            for failure in failures:
                print(paint(f"    - {failure}", RED))
            return 1
        print(paint("  SMOKE TEST PASSED", BOLD + GREEN))
        print(paint(f"    handshake ok · {len(tools)} tools · progress durable ·", GREEN))
        print(paint(f"    wire framing {client.wire_framing} ·", GREEN))
        print(paint("    side effect performed exactly once ·", GREEN))
        print(paint("    agent-reported state correctly withheld from 'resume'", GREEN))
        return 0
    finally:
        client.close()
        if client._errors:
            print()
            print(paint("  server stderr:", DIM))
            for line in client._errors[-10:]:
                print(paint(f"    {line}", DIM))
        shutil.rmtree(DB_PATH + "-journal", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

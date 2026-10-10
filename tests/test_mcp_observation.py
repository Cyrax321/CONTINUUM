"""What the server records about who connected, and what it must never do with it.

The server reads ``clientInfo.name`` from the handshake to authorize a
mutating tool and then discards it, so a registration that guessed the wrong
name leaves the agent silently read-only. These tests cover the record that
replaces the discard.

The one invariant that makes keeping it safe is that an observed name is a
claim by whoever connected, so it may never grant anything. Every test that
touches authorization says so out loud: a name nobody allowlisted is refused,
whatever the observation file says about it, and a corrupted file resolves to
"unknown" rather than to permission.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from continuum.mcp.observation import (
    MAX_OBSERVED_CLIENTS,
    MAX_OBSERVED_NAME_LENGTH,
    declared_client_name,
    observation_path,
    read_observed_clients,
    record_observed_client,
)
from continuum.storage import SQLiteStorage

SRC_DIR = Path(__file__).resolve().parents[1] / "src"

#: A name no test allowlists. Used wherever the point is that a recorded name
#: buys nothing.
UNLISTED = "cursor-vscode"


def _connect(
    db: Path,
    client_name: str | None,
    allow: str | None,
    tmp_path: Path,
    *,
    run_id: str = "no-such-run-xyz",
    goal: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Drive a real server over stdio: handshake, then one mutating call.

    Returns the tool's own text and the observations the connection left. A
    real spawn rather than a direct tool call, because the thing under test is
    what the transport hands the server from the wire: a name set on a fake
    context would not prove the handshake path records anything.

    ``client_name=None`` declares a blank name. A handshake with no
    ``clientInfo`` at all never reaches a tool call: the SDK rejects it as
    invalid params during ``initialize``, so it is not a connection this
    server has to reason about.

    Passing ``goal`` creates a real run, which is what makes the event-log
    check meaningful: without one the call fails before writing anything and
    the absence of a client name in the log would prove nothing.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    if allow is None:
        env.pop("CONTINUUM_MCP_MUTATING_CLIENTS", None)
    else:
        env["CONTINUUM_MCP_MUTATING_CLIENTS"] = allow

    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "continuum.mcp", "--db", str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None

    def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    client_info = {"name": "   " if client_name is None else client_name, "version": "1"}
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": client_info,
            },
        }
    )
    assert json.loads(process.stdout.readline())["result"]["serverInfo"]["name"] == "continuum-mcp"
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    arguments: dict[str, Any] = {"run_id": run_id, "completed": 1}
    if goal is not None:
        arguments["goal"] = goal
    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "continuum_record_progress", "arguments": arguments},
        }
    )
    answer = json.loads(process.stdout.readline())
    process.stdin.close()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - the server always exits here
        process.kill()
    return answer["result"]["content"][0]["text"], read_observed_clients(str(db))


def test_a_connection_records_the_client_name_it_declared(tmp_path: Path) -> None:
    """The value the server throws away is kept, so the doctor can read it.

    This is the whole reason the module exists: ``install`` bakes a guess, and
    the only evidence about the real one is the handshake the server already
    reads and drops.
    """
    db = tmp_path / "continuum.db"

    _, observed = _connect(db, UNLISTED, "someone-else", tmp_path)

    assert list(observed) == [UNLISTED]
    assert observed[UNLISTED]["count"] == 1
    assert observation_path(str(db)).is_file()


def test_an_observed_name_never_grants_a_mutation(tmp_path: Path) -> None:
    """The invariant: recording a name must not make it authorized.

    A client connects claiming a name nobody allowlisted and is refused. The
    observation file now says that name connected, and a second, identical
    call is refused again. If that second call ever succeeds, the observation
    has become an authorization input and the file is a hole: anyone can claim
    to be an allowlisted host and become one.
    """
    db = tmp_path / "continuum.db"

    first, observed = _connect(db, UNLISTED, allow="claude-code", tmp_path=tmp_path)
    assert "is not permitted to use the mutating tool" in first, first
    assert list(observed) == [UNLISTED], "the refused connection was still recorded"

    # The file now names the caller. Authorization must not have moved.
    second, _ = _connect(db, UNLISTED, allow="claude-code", tmp_path=tmp_path)
    assert "is not permitted to use the mutating tool" in second, (
        "the observation file granted a mutation to an unallowlisted name"
    )


def test_an_observed_name_does_not_authorize_a_different_caller(tmp_path: Path) -> None:
    """Recording host A must not let host B borrow A's authorization.

    The observation is per name, not a session-wide fact about the
    connection. Here the allowlist grants ``claude-code`` and the recording
    caller is ``claude-code`` too, so that caller mutates; a second caller
    must still be refused rather than riding on the first one's record.
    """
    db = tmp_path / "continuum.db"

    allowed_text, observed = _connect(db, "claude-code", "claude-code", tmp_path)
    assert "no such run" in allowed_text, allowed_text
    assert list(observed) == ["claude-code"]

    refused_text, _ = _connect(db, UNLISTED, "claude-code", tmp_path)
    assert "is not permitted to use the mutating tool" in refused_text, refused_text


def test_an_unconfigured_server_records_the_caller_and_still_refuses(tmp_path: Path) -> None:
    """With nothing allowlisted, the deny-everything default still holds.

    The observation file records who turned up even when the answer is always
    no, which is exactly when an operator most wants to see the name the host
    really sends.
    """
    db = tmp_path / "continuum.db"

    text, observed = _connect(db, UNLISTED, allow=None, tmp_path=tmp_path)

    assert "not permitted" in text, text
    assert list(observed) == [UNLISTED]


def test_an_unidentified_connection_records_nothing(tmp_path: Path) -> None:
    """A handshake declaring a blank name leaves no observation behind.

    Guessing here would be worse than silence: recording the blank string
    would put a name in the file that no host actually uses, and the doctor
    would then report a mismatch against it. The refusal itself is authz's
    business; what matters here is that nothing is written down.
    """
    db = tmp_path / "continuum.db"

    text, observed = _connect(db, None, allow="claude-code", tmp_path=tmp_path)

    assert "not permitted" in text, text
    assert observed == {}
    assert not observation_path(str(db)).exists(), "a blank name must not create the file"


def test_the_observed_name_never_reaches_the_hash_chained_event_log(tmp_path: Path) -> None:
    """The side file exists precisely because the event log is the trust anchor.

    A run's events are hashed and replayed, and agent-reported facts already
    have to carry ``Origin.EXTERNAL_AGENT``. A self-asserted client name has
    no business in that chain: every reader downstream would treat it as an
    attested fact about the run.

    The connection is allowlisted and creates a real run, so the log genuinely
    has events in it. Asserting on an empty log would pass whatever this
    module did.
    """
    db = tmp_path / "continuum.db"

    text, observed = _connect(
        db, "claude-code", "claude-code", tmp_path, run_id="run_1", goal="ship it"
    )
    assert list(observed) == ["claude-code"], "the connection really was recorded"

    storage = SQLiteStorage(str(db))
    try:
        events = storage.read_events("run_1")
    finally:
        storage.close()

    assert events, "the run must have events, or this assertion proves nothing"
    rendered = json.dumps(
        [
            {"type": str(getattr(event, "type", "")), "payload": getattr(event, "payload", {})}
            for event in events
        ],
        default=str,
    )
    assert "claude-code" not in rendered
    assert text, "the tool call should have succeeded"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        pytest.param("{not json", "unparseable", id="malformed-json"),
        pytest.param(json.dumps({"schema": 99}), "unknown schema", id="wrong-schema"),
        pytest.param(json.dumps({"observed_clients": "nope"}), "wrong shape", id="not-a-dict"),
        pytest.param(json.dumps({"schema": 1, "observed_clients": [1, 2]}), "a list", id="list"),
        pytest.param("", "empty", id="empty-file"),
    ],
)
def test_a_poisoned_observation_file_fails_toward_unknown(
    tmp_path: Path, payload: str, why: str
) -> None:
    """A damaged file yields nothing, never permission.

    Every unreadable shape resolves to the empty reading, which the doctor
    treats as "no host has been observed". Anything that resolved toward
    "allow" here would turn a corrupted diagnostic file into a security
    boundary.
    """
    path = observation_path(str(tmp_path / "continuum.db"))
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    assert read_observed_clients(str(tmp_path / "continuum.db")) == {}, why


def test_a_corrupted_file_is_replaced_rather_than_appended_to(tmp_path: Path) -> None:
    """The next connection repairs an unreadable file instead of failing.

    A diagnostic that cannot recover would stay broken until someone deleted
    it by hand, which is the kind of thing nobody does.
    """
    db = tmp_path / "continuum.db"
    path = observation_path(str(db))
    path.parent.mkdir(parents=True)
    path.write_text("{truncated", encoding="utf-8")

    record_observed_client(str(db), "claude-code")

    assert list(read_observed_clients(str(db))) == ["claude-code"]


def test_a_file_that_cannot_be_written_never_breaks_a_connection(tmp_path: Path) -> None:
    """Losing the observation must cost a diagnosis, not the connection.

    The recorder is best effort by design: an unwritable directory has to
    leave the server serving rather than turn a diagnostic into an outage.
    """
    db = tmp_path / "continuum.db"
    blocker = tmp_path / ".continuum"
    blocker.write_text("a file where the directory needs to be", encoding="utf-8")

    record_observed_client(str(db), "claude-code")  # must not raise

    assert observation_path(str(db)).parent.is_file()


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"clientInfo": {"name": "  cursor  "}}, "cursor", id="trimmed"),
        pytest.param({"clientInfo": {"name": ""}}, None, id="blank"),
        pytest.param({"clientInfo": {"name": "   "}}, None, id="whitespace"),
        pytest.param({"clientInfo": {}}, None, id="no-name"),
        pytest.param({"clientInfo": None}, None, id="null-clientinfo"),
        pytest.param({}, None, id="no-clientinfo"),
        pytest.param({"clientInfo": {"name": 17}}, None, id="non-string"),
        pytest.param(
            {"clientInfo": {"name": "x" * (MAX_OBSERVED_NAME_LENGTH + 1)}}, None, id="too-long"
        ),
        pytest.param(None, None, id="no-params"),
        pytest.param("not-a-mapping", None, id="not-a-mapping"),
    ],
)
def test_a_malformed_handshake_yields_no_name(params: Any, expected: str | None) -> None:
    """Every unusable ``clientInfo`` shape resolves to "unknown".

    The middleware reads raw params before the SDK validates them, so the
    shapes below all arrive in practice. Each one must record nothing rather
    than a placeholder the doctor could mistake for a real caller.
    """
    assert declared_client_name(params) == expected


def test_a_list_of_names_in_one_value_is_recorded_individually(tmp_path: Path) -> None:
    """An allowlist naming three hosts records three names, not one string.

    ``authz`` splits on commas and whitespace, so ``"a, b"`` authorizes both
    ``a`` and ``b``. Recording the raw value would make the doctor compare
    ``"a, b"`` against ``"b"`` and report a healthy registration as a mismatch.
    """
    db = tmp_path / "continuum.db"

    record_observed_client(str(db), "cursor, gemini-cli claude-code")

    assert sorted(read_observed_clients(str(db))) == ["claude-code", "cursor", "gemini-cli"]


def test_the_file_cannot_grow_without_bound(tmp_path: Path) -> None:
    """A client inventing a name per connection cannot grow the file forever.

    The entries are capped and the least recently seen are evicted first, so
    the names a workstation actually uses survive the flood.
    """
    db = tmp_path / "continuum.db"

    for index in range(MAX_OBSERVED_CLIENTS + 10):
        record_observed_client(str(db), f"client-{index:03d}")

    clients = read_observed_clients(str(db))
    assert len(clients) == MAX_OBSERVED_CLIENTS
    assert "client-000" not in clients, "the oldest entry should have been evicted"
    assert f"client-{MAX_OBSERVED_CLIENTS + 9:03d}" in clients


def test_repeated_connections_count_rather_than_duplicate(tmp_path: Path) -> None:
    """A host that reconnects is one entry with a count, not many entries.

    Useful when several hosts share one project: the count and the timestamp
    are what let the doctor say "this host has connected", rather than
    reporting whatever happened to be written last.
    """
    db = tmp_path / "continuum.db"

    for _ in range(3):
        record_observed_client(str(db), "claude-code")

    clients = read_observed_clients(str(db))
    assert list(clients) == ["claude-code"]
    assert clients["claude-code"]["count"] == 3


def test_a_corrupt_count_does_not_break_the_next_write(tmp_path: Path) -> None:
    """A hand-edited count is replaced by a number rather than propagated.

    The file is readable by anyone, so its contents are input, not state this
    module can assume. A count that is a string must not turn the next
    increment into string concatenation.
    """
    db = tmp_path / "continuum.db"
    path = observation_path(str(db))
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema": 1, "observed_clients": {"claude-code": {"count": "many"}}}),
        encoding="utf-8",
    )

    record_observed_client(str(db), "claude-code")

    assert read_observed_clients(str(db))["claude-code"]["count"] == 1

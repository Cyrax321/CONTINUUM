"""Fail-open ingestion of external risk streams (issue #1425).

A monitoring feed -- a SNAGLINE sidecar, a webhook stream, a background watchdog
-- is an external witness, not a gatekeeper. The property under test is what
makes that safe: the feed may crash, drop its connection, emit torn lines, send
malformed JSON or bytes that are not text at all, and none of it may reach the
run it is reporting on. Corrupt records are dropped and counted, the batch is
bounded, and the session outlives every one of them.

Risk events do not project (#303), so the tests also assert the one thing that
keeps a sick feed from costing the run anything: no amount of garbage it sends
can leave the log unprojectable or hold the run's transaction.
"""

from __future__ import annotations

import io
import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Origin
from continuum.recovery.risk import (
    DEFAULT_RISK_BATCH_LIMIT,
    MAX_RISK_RECORD_BYTES,
    ingest_risk_stream,
)
from continuum.serve import server as serve_module
from continuum.serve.server import SidecarHTTP, SidecarServer
from continuum.storage import SQLiteStorage, open_storage

TORN = "this line is not json"
BINARY = b"\x00\x01\xfe\xff garbage from a dead probe"
GOOD = {"trigger": "loop", "score": 0.8}


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    with open_storage(":memory:") as storage:
        yield storage


def _run(store: SQLiteStorage, run_id: str = "r1") -> str:
    from continuum.models import Run

    store.create_run_started(Run(run_id=run_id, goal="stay alive"))
    return run_id


def risk_events(store: SQLiteStorage, run_id: str = "r1") -> list[object]:
    return [e for e in store.read_events(run_id) if e.type is EventType.RISK_OBSERVED]


# --- a corrupt record costs only itself ------------------------------------- #


def test_a_torn_line_drops_one_record_and_keeps_the_rest(store: SQLiteStorage) -> None:
    run_id = _run(store)
    records = [json.dumps(GOOD), TORN, json.dumps({"trigger": "meltdown"})]

    summary = ingest_risk_stream(store, run_id, records)

    assert summary["accepted"] == 2
    assert summary["dropped"] == 1
    assert summary["dropped_by_reason"] == {"invalid_json": 1}
    assert len(risk_events(store, run_id)) == 2


def test_random_binary_garbage_is_dropped_not_raised(store: SQLiteStorage) -> None:
    run_id = _run(store)
    records = [BINARY, json.dumps(GOOD), b"\x89PNG\r\n\x1a\n" * 4]

    summary = ingest_risk_stream(store, run_id, records)

    assert summary["accepted"] == 1
    assert summary["dropped_by_reason"]["undecodable_utf8"] == 2
    assert len(risk_events(store, run_id)) == 1


def test_malformed_unicode_drops_only_its_own_line(store: SQLiteStorage) -> None:
    """A byte the codec refuses must not take the file down with it (#1425).

    Decoding per record is the whole point: a whole-file decode lets one bad byte
    anywhere in a long stream destroy the import, and the record next to it is
    what had the observation.
    """
    run_id = _run(store)
    records = [
        b'{"trigger": "loop"}\n',  # a record carrying its own newline still parses
        b'{"trigger": "error_cascade", "detail": "\xff\xfe"}',  # bytes, not text
        b'{"trigger": "meltdown"}',
    ]

    summary = ingest_risk_stream(store, run_id, records)

    assert summary["accepted"] == 2
    assert summary["dropped"] == 1
    assert summary["dropped_by_reason"] == {"undecodable_utf8": 1}


def test_bytes_that_are_not_utf8_at_all_are_classified(store: SQLiteStorage) -> None:
    run_id = _run(store)
    summary = ingest_risk_stream(store, run_id, [b'{"trigger": "loop"}', b"\xff\xfe", b"\x80\x81"])
    assert summary["accepted"] == 1
    assert summary["dropped_by_reason"]["undecodable_utf8"] == 2


@pytest.mark.parametrize("record", ["[1, 2, 3]", '"just a string"', "5", "null", "true"])
def test_json_that_is_not_an_object_is_dropped(store: SQLiteStorage, record: str) -> None:
    run_id = _run(store)
    summary = ingest_risk_stream(store, run_id, [record, json.dumps(GOOD)])
    assert summary["accepted"] == 1
    assert summary["dropped_by_reason"] == {"not_an_object": 1}


def test_a_record_the_schema_refuses_is_dropped_and_reported(store: SQLiteStorage) -> None:
    run_id = _run(store)
    # The schema is tolerant by design -- it clamps scores, truncates ids, and
    # the payload builder passes only the fields it knows, so a schema refusal
    # on this path is a monitor that did not name a risk class. That is the one
    # refusal worth pinning, because it is also the one the projector would
    # reject if it ever reached the log.
    records = [
        {"trigger": ""},  # unnamed
        {"score": 0.5},  # no trigger at all
        {"trigger": "   "},  # whitespace only is still unnamed
        json.dumps(GOOD),
    ]

    summary = ingest_risk_stream(store, run_id, records)

    assert summary["accepted"] == 1
    assert summary["dropped"] == 3
    assert summary["dropped_by_reason"]["schema_validation"] == 3
    assert len(risk_events(store, run_id)) == 1


def test_a_write_failure_drops_one_record_and_does_not_stop_the_batch(
    store: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage trouble is a dropped record, not an exception the caller has to handle."""
    run_id = _run(store)
    calls: list[str] = []

    real_append = store.append_event

    def flapping(run_id_: str, type_: EventType, payload=None, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(type_.value)
        if len(calls) == 1:
            raise RuntimeError("database is busy")
        return real_append(run_id_, type_, payload, **kwargs)

    monkeypatch.setattr(store, "append_event", flapping)
    summary = ingest_risk_stream(store, run_id, [json.dumps(GOOD), json.dumps(GOOD)])

    assert summary["accepted"] == 1
    assert summary["dropped_by_reason"] == {"write_failed": 1}


def test_a_blank_line_is_a_separator_not_a_drop(store: SQLiteStorage) -> None:
    run_id = _run(store)
    records = [json.dumps(GOOD), "", b"", "   ", b"   \n", json.dumps({"trigger": "meltdown"})]

    summary = ingest_risk_stream(store, run_id, records)

    assert summary["accepted"] == 2
    assert summary["dropped"] == 0
    assert summary["skipped"] == 4


def test_drops_are_counted_on_the_process_metrics(store: SQLiteStorage) -> None:
    from continuum.observability import RISKS_DROPPED, RISKS_INGESTED, get_metrics, reset_metrics

    reset_metrics()
    run_id = _run(store)
    ingest_risk_stream(store, run_id, [json.dumps(GOOD), TORN, json.dumps(GOOD)])

    counters = get_metrics().counters
    assert counters[RISKS_INGESTED] == 2
    assert counters[RISKS_DROPPED] == 1


# --- the batch is bounded ---------------------------------------------------- #


def test_the_batch_cap_reports_the_remainder_instead_of_reading_forever(
    store: SQLiteStorage,
) -> None:
    run_id = _run(store)
    feed = (json.dumps(GOOD) for _ in range(1000))  # lazily evaluated, never materialised

    summary = ingest_risk_stream(store, run_id, feed, limit=10)

    assert summary["accepted"] == 10
    assert summary["truncated"] is True
    assert summary["limit"] == 10
    assert len(risk_events(store, run_id)) == 10


def test_the_default_cap_is_applied(store: SQLiteStorage) -> None:
    run_id = _run(store)
    feed = (json.dumps(GOOD) for _ in range(DEFAULT_RISK_BATCH_LIMIT + 50))

    summary = ingest_risk_stream(store, run_id, feed)

    assert summary["accepted"] == DEFAULT_RISK_BATCH_LIMIT
    assert summary["truncated"] is True


def test_a_record_past_the_record_cap_is_refused_without_a_parse(store: SQLiteStorage) -> None:
    run_id = _run(store)
    oversize = b"x" * (MAX_RISK_RECORD_BYTES + 1024)

    summary = ingest_risk_stream(store, run_id, [oversize, json.dumps(GOOD)])

    assert summary["accepted"] == 1
    assert summary["dropped_by_reason"] == {"oversized_record": 1}


def test_no_limit_takes_the_whole_batch(store: SQLiteStorage) -> None:
    run_id = _run(store)
    feed = [json.dumps(GOOD)] * 3

    summary = ingest_risk_stream(store, run_id, feed, limit=None)

    assert summary["accepted"] == 3
    assert summary["truncated"] is False


# --- nothing a feed sends can reach the run --------------------------------- #


def test_risk_events_are_stamped_external_monitor(store: SQLiteStorage) -> None:
    run_id = _run(store)
    ingest_risk_stream(store, run_id, [json.dumps(GOOD)])
    assert {e.source for e in risk_events(store, run_id)} == {Origin.EXTERNAL_MONITOR}


def test_garbage_never_leaves_the_run_unprojectable(store: SQLiteStorage) -> None:
    """The fold must still accept the log after a feed sent its worst (#1425).

    This is the property that keeps fail-open honest: dropping the record is not
    enough if the accept path could still have written something the projector
    refuses, because every projecting surface for the run would then stay dead
    while the agent kept authorising side effects.
    """
    from continuum.state.semantic import project

    run_id = _run(store)
    ingest_risk_stream(
        store,
        run_id,
        [TORN, BINARY, json.dumps({"trigger": "unknown_class"}), "null", json.dumps(GOOD)],
    )
    state = project(run_id, store.read_events(run_id))
    assert state.progress.completed == 0
    assert state.run_id == run_id


def test_the_summary_carries_where_the_batch_got_to(store: SQLiteStorage) -> None:
    run_id = _run(store)
    from continuum.models import Run

    store.create_run_started(Run(run_id="r2", goal="second run"))
    first = ingest_risk_stream(store, run_id, [json.dumps(GOOD)])
    second = ingest_risk_stream(store, "r2", [json.dumps(GOOD), json.dumps(GOOD)])

    assert first["last_sequence"] == max(e.sequence for e in risk_events(store, run_id))
    assert second["last_sequence"] == max(e.sequence for e in risk_events(store, "r2"))


def test_drop_samples_are_bounded_and_one_line_each(store: SQLiteStorage) -> None:
    run_id = _run(store)
    summary = ingest_risk_stream(store, run_id, [TORN] * 100)
    samples = summary["dropped_samples"]

    assert len(samples) <= 8
    for sample in samples:
        assert "\n" not in sample["message"]
        assert sample["reason"] == "invalid_json"
    assert summary["dropped"] == 100


# --- the sidecar wire -------------------------------------------------------- #


def make_server() -> SidecarServer:
    return SidecarServer(database=":memory:")


def test_the_wire_method_ingests_a_stream_and_answers_the_accounting() -> None:
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 1, "goal": "g"})
    stream = "\n".join([json.dumps(GOOD), TORN, json.dumps({"trigger": "meltdown"})]) + "\n"

    result = srv.dispatch("ingest_risks", {"run_id": "r1", "stream": stream})

    assert result["accepted"] == 2
    assert result["dropped_by_reason"] == {"invalid_json": 1}
    assert len(risk_events(srv.storage, "r1")) == 2
    srv.close()


def test_the_wire_method_accepts_already_parsed_records() -> None:
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 1, "goal": "g"})

    result = srv.dispatch("ingest_risks", {"run_id": "r1", "risks": [GOOD, TORN]})

    assert result["accepted"] == 1
    assert result["limit"] == DEFAULT_RISK_BATCH_LIMIT
    srv.close()


def test_a_torn_line_does_not_end_the_stdio_session() -> None:
    """One bad line in a batch must not kill the requests queued behind it."""
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 1, "goal": "g"})
    requests = [
        {
            "id": 1,
            "method": "ingest_risks",
            "params": {"run_id": "r1", "stream": '{"trigger": "loop"}\n' + TORN},
        },
        {"id": 2, "method": "resume", "params": {"run_id": "r1"}},
    ]
    out = io.StringIO()

    exit_code = srv.serve_stdio(io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out)

    assert exit_code == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[0]["result"]["accepted"] == 1
    assert "mode" in responses[1]["result"]
    srv.close()


def test_an_unknown_run_is_refused_so_it_cannot_leave_an_orphan_log() -> None:
    from continuum.serve.server import BadParams

    srv = make_server()
    with pytest.raises(BadParams) as excinfo:
        srv.dispatch("ingest_risks", {"run_id": "ghost", "risks": [GOOD]})
    assert excinfo.value.code == "bad_params"
    assert "ghost" in str(excinfo.value)
    assert srv.storage.read_events("ghost") == []
    srv.close()


@pytest.mark.parametrize(
    "params",
    [
        {"run_id": "r1", "risks": "not a list"},
        {"run_id": "r1", "stream": 5},
        {"run_id": "r1", "limit": -1},
        {"run_id": "r1", "limit": True},
        {"run_id": "r1", "limit": "ten"},
        {},
    ],
)
def test_bad_param_shapes_are_protocol_errors(params: dict[str, object]) -> None:
    from continuum.serve.server import BadParams

    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 1, "goal": "g"})
    with pytest.raises(BadParams):
        srv.dispatch("ingest_risks", params)
    srv.close()


# --- the HTTP transport: torn lines, truncation, timeouts ------------------- #


@pytest.fixture
def http_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # The read timeout is patched low so a stalled body is answered in the time a
    # test can wait, rather than the 30s a production caller gets.
    monkeypatch.setattr(serve_module, "SIDECAR_BODY_TIMEOUT_SECONDS", 1.0)
    db = str(tmp_path / "risk-stream.db")
    sidecar = SidecarServer(database=db)
    sidecar.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 1, "goal": "g"})
    http = SidecarHTTP(sidecar, port=0)
    import threading

    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{http.port}"
    finally:
        http.shutdown()
        sidecar.close()


def post(addr: str, params: dict[str, object]) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(
        f"http://{addr}/ingest_risks",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def _speak(addr: str, request: bytes, *, read_timeout: float = 10.0) -> bytes:
    """Send exact bytes, read everything that comes back, return the whole response."""
    host, _, port = addr.partition(":")
    with socket.create_connection((host, int(port)), timeout=read_timeout) as sock:
        sock.settimeout(read_timeout)
        sock.sendall(request)
        received = b""
        while True:
            try:
                data = sock.recv(65536)
            except ConnectionResetError:
                break
            if not data:
                break
            received += data
    return received


_STATUS = re.compile(rb"HTTP/1\.[01] (\d{3})")


def test_http_ingests_a_stream_with_a_torn_line(http_server: str, tmp_path: Path) -> None:
    stream = "\n".join([json.dumps(GOOD), TORN, json.dumps({"trigger": "meltdown"})])
    status, body = post(http_server, {"run_id": "r1", "stream": stream})
    assert status == 200, body
    assert body["accepted"] == 2
    assert body["dropped_by_reason"] == {"invalid_json": 1}

    with SQLiteStorage(tmp_path / "risk-stream.db") as store:
        assert len(risk_events(store, "r1")) == 2


def test_http_drops_schema_failures_and_torn_lines(http_server: str) -> None:
    records = [
        {"trigger": "loop", "score": 0.9},
        {"trigger": "", "score": 0.5},  # a monitor must name a risk class
        TORN,
        {"trigger": "error_cascade"},
    ]
    status, body = post(http_server, {"run_id": "r1", "risks": records})
    assert status == 200, body
    assert body["accepted"] == 2
    assert body["dropped_by_reason"] == {"schema_validation": 1, "invalid_json": 1}


def test_http_a_body_that_is_not_utf8_is_answered_not_dropped(http_server: str) -> None:
    """A feed whose encoding broke gets a 400, not a connection that died.

    The decoder raises where the JSON parser would have, so without this arm the
    traceback left the socket closed with no response -- indistinguishable, to
    the feed, from a sidecar that crashed.
    """
    head = (
        b"POST /ingest_risks HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Type: application/json\r\nConnection: close\r\n"
    )
    body = b'{"run_id": "r1", "\xff\xfe'
    raw = _speak(
        http_server,
        head + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
    )
    assert re.search(rb"HTTP/1\.[01] 400", raw), raw
    assert b"invalid JSON body" in raw


def test_http_the_server_still_serves_after_a_body_it_could_not_decode(
    http_server: str,
) -> None:
    _speak(
        http_server,
        (
            b"POST /ingest_risks HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nConnection: close\r\n"
            b"Content-Length: 12\r\n\r\n"
        )
        + b"\xff\xfe\x00\x01\xff\xfe\x00\x01\xff\xfe\x00\x01",
    )
    status, body = post(http_server, {"run_id": "r1", "risks": [GOOD]})
    assert status == 200, body
    assert body["accepted"] == 1


def test_http_an_unknown_run_is_a_400_not_a_500(http_server: str) -> None:
    status, body = post(http_server, {"run_id": "ghost", "risks": [GOOD]})
    assert status == 400
    assert "ghost" in body["error"]


def test_http_a_batch_over_the_cap_reports_the_truncation(http_server: str) -> None:
    status, body = post(http_server, {"run_id": "r1", "risks": [GOOD] * 10, "limit": 3})
    assert status == 200, body
    assert body["accepted"] == 3
    assert body["truncated"] is True


def test_http_a_stalled_body_is_answered_and_closed(http_server: str) -> None:
    """A feed that declared a body it stopped writing is a 408, not a held thread.

    This is the wedge the issue is about: without a bound on the read, a dead
    feed holds its handler for as long as the socket stays open, and the request
    behind it never gets answered.
    """
    partial = b'{"run_id": "r1", "stream": "'
    head = (
        f"POST /ingest_risks HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {len(partial) + 512}\r\nConnection: close\r\n\r\n"
    ).encode()
    raw = _speak(http_server, head + partial, read_timeout=10.0)

    assert _STATUS.search(raw).group(1) == b"408", raw
    assert b"timed out" in raw
    assert b"Connection: close" in raw


def test_http_the_server_still_serves_after_a_stalled_body(http_server: str) -> None:
    _speak(
        http_server,
        (
            b"POST /ingest_risks HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 4096\r\nConnection: close\r\n\r\n"
        )
        + b'{"run_id": "r1", "stream": "',
    )
    status, body = post(http_server, {"run_id": "r1", "risks": [GOOD]})
    assert status == 200, body
    assert body["accepted"] == 1


def test_http_a_feed_that_dies_mid_body_does_not_wedge_the_server(http_server: str) -> None:
    """A truncated connection is closed, and the next request is still served."""
    host, _, port = http_server.partition(":")
    sock = socket.create_connection((host, int(port)), timeout=10)
    sock.sendall(
        (
            b"POST /ingest_risks HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Length: 2048\r\nConnection: close\r\n\r\n"
        )
        + b'{"run_id": "r1", "stream": "'
    )
    # Reset on close so the server sees a dead peer rather than a clean finish.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    sock.close()

    status, body = post(http_server, {"run_id": "r1", "risks": [GOOD]})
    assert status == 200, body
    assert body["accepted"] == 1


# --- the CLI ----------------------------------------------------------------- #


def _run_cli(argv: list[str], stdin: io.StringIO | None = None) -> tuple[int, str, str]:
    import sys

    from continuum.cli import main

    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = stdin
    try:
        code = main(argv, out=out, err=err)
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def cli_db(tmp_path: Path) -> str:
    from continuum.models import Run

    db = str(tmp_path / "cli.db")
    with SQLiteStorage(db) as store:
        store.create_run_started(Run(run_id="r1", goal="stay alive"))
    return db


def test_cli_imports_a_file_and_reports_the_drops(cli_db: str, tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_bytes(
        b'{"trigger": "loop"}\n'
        + TORN.encode()
        + b"\n"
        + b'{"trigger": "meltdown"}\n'
        + BINARY
        + b"\n"
        + b'{"trigger": "token_runaway"}\n'
    )

    code, out, err = _run_cli(
        ["--db", cli_db, "import-risks", "r1", "--file", str(stream), "--json"]
    )

    assert code == 0, err
    summary = json.loads(out)
    assert summary["accepted"] == 3
    assert summary["dropped_by_reason"] == {"invalid_json": 1, "undecodable_utf8": 1}
    assert "dropped 2 of 5" in err
    with SQLiteStorage(cli_db) as store:
        assert len(risk_events(store, "r1")) == 3


def test_cli_reads_a_stream_from_stdin(cli_db: str) -> None:
    stdin = io.StringIO('{"trigger": "loop"}\n' + TORN + '\n{"trigger": "meltdown"}\n')

    code, out, err = _run_cli(["--db", cli_db, "import-risks", "r1", "--json"], stdin=stdin)

    assert code == 0, err
    assert json.loads(out)["accepted"] == 2


def test_cli_text_mode_still_drops_a_torn_line(cli_db: str) -> None:
    stdin = io.StringIO('{"trigger": "loop"}\n' + TORN + "\n")

    code, out, _err = _run_cli(["--db", cli_db, "import-risks", "r1"], stdin=stdin)

    assert code == 0
    assert "Imported 1 risk signal(s) into r1 (1 dropped, 0 blank)" in out


def test_cli_corruption_is_exit_zero_so_the_pipeline_stays_green(
    cli_db: str, tmp_path: Path
) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(TORN + "\n")

    code, out, err = _run_cli(["--db", cli_db, "import-risks", "r1", "--file", str(stream)])

    assert code == 0
    assert "Imported 0 risk signal(s) into r1 (1 dropped, 0 blank)" in out
    assert "warning:" in err


def test_cli_a_missing_file_is_an_operator_error(cli_db: str, tmp_path: Path) -> None:
    code, _out, err = _run_cli(
        ["--db", cli_db, "import-risks", "r1", "--file", str(tmp_path / "nope.jsonl")]
    )
    assert code == 1
    assert "file not found" in err


def test_cli_an_unknown_run_is_an_error(cli_db: str, tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps(GOOD) + "\n")

    code, _out, err = _run_cli(["--db", cli_db, "import-risks", "ghost-run", "--file", str(stream)])
    assert code != 0
    assert "ghost-run" in err


def test_cli_a_run_with_a_stream_imported_is_still_projectable(cli_db: str, tmp_path: Path) -> None:
    """The CLI writes the same events the wire does, so the same guarantee holds."""
    from continuum.state.semantic import project

    stream = tmp_path / "stream.jsonl"
    stream.write_bytes(b'{"trigger": "loop"}\n' + BINARY + b"\n")

    code, _out, _err = _run_cli(
        ["--db", cli_db, "import-risks", "r1", "--file", str(stream), "--json"]
    )
    assert code == 0

    with SQLiteStorage(cli_db) as store:
        assert project("r1", store.read_events("r1")).run_id == "r1"

"""Out-of-band storage for oversized event payloads (issue #254).

Payloads above ``CONTINUUM_PAYLOAD_OFFLOAD_BYTES`` are written to a
content-addressed blob beside the database and replaced inline with a small
marker. The contract these tests pin:

* the feature is off unless the key is set, and off means the row stores
  byte-identical JSON to what it stored before the codec existed -- including
  for a payload that happens to use the marker keys;
* reads rehydrate transparently, so no caller of ``read_events``,
  ``read_archived_events`` or ``read_all_events`` knows the codec exists;
* a blob the log still references but that an operator deleted is refused as
  ``CorruptedRecord`` naming the digest, never substituted with an empty
  payload;
* compaction keeps the payload reachable, because the archived row keeps the
  marker and the blob is content-addressed;
* ``verify --deep`` reports the blob itself missing or tampered, instead of
  the chain's generic unreadable-record finding.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.checkpoint import CheckpointManager
from continuum.cli import ExitCode, main
from continuum.events import Event, EventType
from continuum.models import Origin, Run
from continuum.storage import SQLiteStorage
from continuum.storage.base import CorruptedRecord
from continuum.storage.payload import (
    OFFLOAD_THRESHOLD_ENV,
    BlobStore,
    offload_threshold,
)

#: Comfortably over every threshold used below, so the payload is offloaded
#: wherever a test sets a threshold.
BIG = 4096

#: The threshold the ``offloaded`` fixture uses: small enough that the tests
#: stay quick, large enough that a small payload stays inline by contrast.
THRESHOLD = 200


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _no_offload_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test states its own threshold; none inherits the ambient one.

    The threshold is read from the environment at construction, so a developer
    running the suite with the key exported would otherwise see the codec on
    in tests that expect it off.
    """
    monkeypatch.delenv(OFFLOAD_THRESHOLD_ENV, raising=False)


@pytest.fixture
def db(tmp_path: Path) -> str:
    """Path of a file-backed database. The store is built per test, with the
    threshold the test wants -- the codec reads the key at construction."""
    return str(tmp_path / "run.db")


@pytest.fixture
def offloaded(monkeypatch: pytest.MonkeyPatch, db: str) -> str:
    """A database with the codec on, seeded with one run and its start event."""
    monkeypatch.setenv(OFFLOAD_THRESHOLD_ENV, str(THRESHOLD))
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="r1", goal="g"))
        store.append_event("r1", EventType.RUN_STARTED, {"goal": "g"}, source=Origin.HUMAN)
    return db


def _row_payload(db: str, *, table: str = "events", sequence_desc: bool = True) -> object:
    """The payload column as stored, before rehydration."""
    conn = sqlite3.connect(db)
    try:
        order = "DESC" if sequence_desc else "ASC"
        row = conn.execute(
            f"SELECT payload FROM {table} ORDER BY sequence {order} LIMIT 1"  # noqa: S608
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def _blobs(db: str) -> list[Path]:
    return sorted(Path(db + ".blobs").iterdir())


# -- the config key ------------------------------------------------------- #


@pytest.mark.parametrize("value", ["", "0", "-1", "not-a-number"])
def test_the_codec_stays_off_for_an_unset_or_invalid_threshold(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Off is the default, and a malformed value is off rather than ignored.

    A typo in the key must not look like the feature working while it does
    nothing, so an unparseable value disables rather than raising.
    """
    if value:
        monkeypatch.setenv(OFFLOAD_THRESHOLD_ENV, value)
    else:
        monkeypatch.delenv(OFFLOAD_THRESHOLD_ENV, raising=False)
    assert offload_threshold() == 0
    assert not BlobStore(Path("/tmp")).enabled


def test_the_codec_turns_on_for_a_positive_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OFFLOAD_THRESHOLD_ENV, "1024")
    assert offload_threshold() == 1024
    assert BlobStore(Path("/tmp"), threshold=1024).enabled


def test_an_in_memory_database_never_offloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """``:memory:`` has no file to sit beside, so the codec is a no-op there.

    An in-memory database is not durable in the first place; out-of-band
    storage would add a second, separately-undurable copy of nothing.
    """
    monkeypatch.setenv(OFFLOAD_THRESHOLD_ENV, "1")
    with SQLiteStorage(":memory:") as st:
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, {"big": "x" * BIG})
        # Round-trips from the row, and no blob directory was created.
        assert st.read_events("r1")[0].payload == {"big": "x" * BIG}
        assert st._blobs.directory is None


# -- off: byte-identical to the pre-codec row ----------------------------- #


def test_the_row_stores_plain_json_with_the_codec_off(db: str) -> None:
    """Nothing about the stored form changes while the feature is off."""
    payload = {"big": "x" * BIG, "n": 1}
    with SQLiteStorage(db) as st:
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, payload)
    assert _row_payload(db) == payload
    assert not Path(db + ".blobs").exists()


def test_a_marker_shaped_payload_round_trips_with_the_codec_off(db: str) -> None:
    """Off means off: even the marker keys are read as written.

    ``decode`` only interprets a marker while the codec is enabled, so a
    payload that legitimately uses the reserved keys round-trips on a store
    that never opted in. This is what keeps the marker from being a hazard
    for databases written before the feature existed.
    """
    payload = {"__offloaded": "not-a-digest", "keys": ["a"], "bytes": 4}
    with SQLiteStorage(db) as st:
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, payload)
        assert st.read_events("r1")[0].payload == payload


def test_a_payload_that_merely_uses_the_marker_name_is_not_a_blob_reference(
    offloaded: str,
) -> None:
    """The marker is an exact key set, so near-misses stay inline.

    A payload carrying ``__offloaded`` alongside a fourth key is not a marker
    the codec wrote: it is ordinary content that happens to use the name. Both
    the read path and ``verify --deep`` must agree on that, or a near-miss
    would be reported as a blob that never existed.
    """
    near_miss = {"__offloaded": "not-a-digest", "keys": ["a"], "bytes": 4, "extra": 1}
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, near_miss)
        assert st.read_events("r1")[-1].payload == near_miss
        report = st.verify_events("r1", deep=True)
    assert report.ok, [v.detail for v in report.violations]


# -- the roundtrip -------------------------------------------------------- #


def test_an_oversized_payload_round_trips_through_a_blob(offloaded: str) -> None:
    """The reader sees the original payload; only the row is small."""
    payload = {"blob": "x" * BIG, "nested": {"deep": ["a", "b"]}}
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, payload)
        events = list(st.read_events("r1"))
    assert events[-1].payload == payload

    blobs = _blobs(offloaded)
    assert len(blobs) == 1
    marker = _row_payload(offloaded)
    assert isinstance(marker, dict)
    assert set(marker) == {"__offloaded", "keys", "bytes"}
    # The digest is the file name, and the marker reports what it holds.
    assert marker["keys"] == ["blob", "nested"]
    assert marker["bytes"] == len(json.dumps(payload, sort_keys=True).encode())
    assert marker["__offloaded"] == blobs[0].name


def test_a_small_payload_stays_inline(offloaded: str) -> None:
    """Under the threshold the payload is stored in the row, no blob written."""
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"note": "small"})
        assert st.read_events("r1")[-1].payload == {"note": "small"}
    assert not Path(offloaded + ".blobs").exists()


def test_identical_payloads_share_one_blob(offloaded: str) -> None:
    """The digest is the file name, so repeated content costs one file."""
    payload = {"blob": "x" * BIG}
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, dict(payload))
        st.append_event("r1", EventType.ACTION_RECORDED, dict(payload))
        assert len(st.read_events("r1")) == 3
    assert len(_blobs(offloaded)) == 1


def test_the_hash_chain_verifies_across_an_offloaded_payload(offloaded: str) -> None:
    """The event was sealed over the original payload, and still seals.

    Rehydration restores the payload the digest was computed over, so the
    chain audit is unchanged; this is the property that makes the codec safe
    rather than merely compact.
    """
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        report = st.verify_events("r1")
    assert report.ok, [v.detail for v in report.violations]


def test_append_sealed_also_offloads_its_payload(offloaded: str) -> None:
    """The pre-sealed entry point goes through the same codec as append."""
    with SQLiteStorage(offloaded) as st:
        # Chain onto the live head the way a sealed caller must.
        head = st.read_events("r1")[-1]
        event = Event(
            run_id="r1",
            sequence=head.sequence + 1,
            type=EventType.ACTION_RECORDED,
            payload={"blob": "x" * BIG},
            source=Origin.HUMAN,
            prev_hash=head.hash,
        ).sealed()
        st.append_sealed(event)
        assert st.read_events("r1")[-1].payload == {"blob": "x" * BIG}
    assert Path(offloaded + ".blobs").exists()


# -- the failure the codec must not paper over --------------------------- #


def test_a_missing_blob_is_reported_not_substituted(offloaded: str) -> None:
    """Deleting a blob the log references fails loudly, by digest.

    Blob collection is operator-owned: reclaiming space needs a reachability
    scan that is wrong the moment a new event with the same content arrives.
    The run must therefore refuse to read rather than silently present an
    empty payload, which would let a resume proceed having lost the event
    that described what it did.
    """
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        blob = _blobs(offloaded)[0]
        blob.unlink()
        with pytest.raises(CorruptedRecord) as excinfo:
            st.read_events("r1")
    assert blob.name in str(excinfo.value)
    assert "missing" in str(excinfo.value)


def test_a_truncated_blob_is_reported_not_half_read(offloaded: str) -> None:
    """A blob whose contents no longer parse is corruption, not a payload."""
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        _blobs(offloaded)[0].write_bytes(b"{this is not json")
        with pytest.raises(CorruptedRecord):
            st.read_events("r1")


# -- compaction ----------------------------------------------------------- #


def test_compaction_keeps_an_offloaded_payload_readable(offloaded: str) -> None:
    """The archived row keeps the marker and the blob is shared, so it reads.

    No bytes move: the digest is content-addressed, so the archive references
    the same file the live row did. A blob deleted after compaction is still
    refused, because the archived event still needs it.
    """
    with SQLiteStorage(offloaded) as st:
        ledger = ActionLedger(st, "r1")
        outcome = ledger.claim("send_invoice", {"blob": "x" * BIG}, key="invoice:I-1")
        ledger.fail(outcome.key, "timeout", certain=False)
        CheckpointManager(st).checkpoint("r1", trigger="manual", reason="anchor")
        before = [p.name for p in _blobs(offloaded)]
        assert st.compact_run("r1")["archived"] >= 4
        after = [p.name for p in _blobs(offloaded)]
        # Compaction moved rows but no bytes: the archive points at the same
        # digests the live log did. New blobs may appear -- the anchor marker
        # is itself an event -- but nothing already written is rewritten.
        assert set(before) <= set(after)

        # The claim left the live tail: only anchor markers survive.
        assert not any(e.type is EventType.ACTION_RECORDED for e in st.read_events("r1"))
        archived = next(
            e
            for e in st.read_archived_events("r1")
            if e.type is EventType.ACTION_RECORDED and e.payload["action"]["status"] == "unknown"
        )
        assert archived.payload["action"]["arguments"]["blob"] == "x" * BIG
        # The ledger fold is archive-aware, so the claim still guards.
        assert outcome.key in set(ledger.folded())

        # Losing the blob breaks the archived event, not just the live one.
        # The claim, the failure and the anchor marker each wrote their own
        # blob, so take them all: any referenced one gone must refuse the read.
        for blob in _blobs(offloaded):
            blob.unlink()
        with pytest.raises(CorruptedRecord):
            st.read_archived_events("r1")


# -- verify --deep -------------------------------------------------------- #


def test_deep_verify_passes_when_the_blobs_are_present(offloaded: str) -> None:
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        report = st.verify_events("r1", deep=True)
    assert report.ok, [v.detail for v in report.violations]


def test_deep_verify_names_a_missing_blob(offloaded: str) -> None:
    """The deep pass reports the blob, not the event's unreadability."""
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        blob = _blobs(offloaded)[0]
        blob.unlink()
        report = st.verify_events("r1", deep=True)
    kinds = {v.kind for v in report.violations}
    assert "BLOB_MISSING" in kinds
    detail = next(v.detail for v in report.violations if v.kind == "BLOB_MISSING")
    assert blob.name in detail


def test_deep_verify_names_a_tampered_blob(offloaded: str) -> None:
    """A blob rewritten after storage no longer hashes to its digest."""
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        _blobs(offloaded)[0].write_bytes(json.dumps({"blob": "y" * BIG}).encode())
        report = st.verify_events("r1", deep=True)
    assert "BLOB_DIGEST_MISMATCH" in {v.kind for v in report.violations}


def test_deep_verify_reports_an_archived_blob_gone(offloaded: str) -> None:
    """The audit reads both tables, so compaction cannot hide a loss."""
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        CheckpointManager(st).checkpoint("r1", trigger="manual", reason="anchor")
        st.compact_run("r1")
        _blobs(offloaded)[0].unlink()
        report = st.verify_events("r1", deep=True)
    assert not report.ok
    assert "BLOB_MISSING" in {v.kind for v in report.violations}


def test_deep_is_a_noop_on_a_store_that_never_offloaded(db: str) -> None:
    """An un-offloaded run reports nothing, so the flag is always safe."""
    with SQLiteStorage(db) as st:
        st.create_run(Run(run_id="r1", goal="g"))
        st.append_event("r1", EventType.RUN_STARTED, {"goal": "g"})
        report = st.verify_events("r1", deep=True)
    assert report.ok
    assert report.violations == []


# -- the CLI surface ------------------------------------------------------ #


def test_the_cli_verify_deep_flag_reports_blob_loss(
    monkeypatch: pytest.MonkeyPatch, offloaded: str
) -> None:
    """``continuum verify --deep`` exits non-zero and names the digest.

    The CLI builds its own storage, so the threshold must be set for the
    process the command runs in -- the same way an operator would export it.
    """
    monkeypatch.setenv(OFFLOAD_THRESHOLD_ENV, str(THRESHOLD))
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        blob = _blobs(offloaded)[0]
        st.close()
    blob.unlink()

    code, out, _ = run_cli("--db", offloaded, "verify", "r1", "--deep")
    assert code is not ExitCode.OK
    assert "BLOB_MISSING" in out
    assert blob.name in out


def test_the_cli_verify_deep_passes_when_intact(
    monkeypatch: pytest.MonkeyPatch, offloaded: str
) -> None:
    monkeypatch.setenv(OFFLOAD_THRESHOLD_ENV, str(THRESHOLD))
    with SQLiteStorage(offloaded) as st:
        st.append_event("r1", EventType.ACTION_RECORDED, {"blob": "x" * BIG})
        st.close()
    code, out, _ = run_cli("--db", offloaded, "verify", "r1", "--deep")
    assert code is ExitCode.OK, out
    assert "Offloaded payloads checked" in out

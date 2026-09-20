"""Version pinning on observations and claims (issue #241).

Pinning is caller-asserted environment identity (prompt hash, tool schema
hash, model id, policy version) stored verbatim on ACTION_RECORDED and
REASONING_SUMMARY events, and diffed on resume as informational drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.events import EventType
from continuum.models import Run
from continuum.pinning import (
    latest_pinning,
    normalize_pinning,
    pinning_drift,
)
from continuum.storage import SQLiteStorage


def test_normalize_passes_known_keys_and_drops_nones() -> None:
    out = normalize_pinning(
        {
            "prompt_sha256": "abc",
            "model_id": "gpt-4o-mini",
            "policy_version": None,
        }
    )
    assert out == {"prompt_sha256": "abc", "model_id": "gpt-4o-mini"}


def test_normalize_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown pinning key"):
        normalize_pinning({"free_text": "the whole transcript"})


def test_normalize_rejects_oversized_values() -> None:
    with pytest.raises(ValueError, match="store the hash"):
        normalize_pinning({"prompt_sha256": "x" * 300})


def test_drift_detects_changes_new_pins_and_unpins() -> None:
    lines = pinning_drift(
        {"prompt_sha256": "aaa", "model_id": "m1"},
        {"prompt_sha256": "bbb", "tool_schema_sha256": "zzz"},
    )
    assert any("prompt_sha256" in ln and "changed" in ln for ln in lines)
    assert any("newly pinned" in ln and "tool_schema_sha256" in ln for ln in lines)
    assert any("unpinned" in ln and "model_id" in ln for ln in lines)


def test_identical_pinning_produces_no_drift() -> None:
    pins = {"prompt_sha256": "same", "model_id": "m"}
    assert pinning_drift(pins, pins) == []


# --- storage round trip ------------------------------------------------------------ #


def test_claim_stores_pinning_in_the_started_record(tmp_path: Path) -> None:
    path = str(tmp_path / "p.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        ledger = ActionLedger(store, "run_1")
        ledger.claim(
            "send_invoice",
            {},
            key="invoice:1",
            pinning={"prompt_sha256": "deadbeef", "model_id": "m1"},
        )

    with SQLiteStorage(path) as store:
        events = [e for e in store.read_events("run_1") if e.type is EventType.ACTION_RECORDED]
    started = [e for e in events if e.payload["action"]["status"] == "started"]
    assert started[0].payload["pinning"] == {"prompt_sha256": "deadbeef", "model_id": "m1"}


def test_latest_pinning_returns_newest_non_empty(tmp_path: Path) -> None:
    path = str(tmp_path / "p.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        ledger = ActionLedger(store, "run_1")
        o1 = ledger.claim("send_invoice", {}, key="i:1", pinning={"model_id": "m1"})
        ledger.complete(o1.key)
        o2 = ledger.claim("send_invoice", {}, key="i:2", pinning={"model_id": "m2"})
        del o2
        # An unpinned claim must not erase the newest recorded pinning.
        o3 = ledger.claim("send_invoice", {}, key="i:3")
        del o3

    with SQLiteStorage(path) as store:
        events = store.read_events("run_1")
    latest = latest_pinning(events)
    assert latest.get("model_id") == "m2"


# --- resume drift surfacing ---------------------------------------------------------- #


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "pin.db")


def _seed_pinned_run(db: str) -> None:
    import io

    from continuum.cli import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    code = cli_main(["--db", db, "--json", "start", "pinned", "--goal", "g"], out=out, err=err)
    del code
    with SQLiteStorage(db) as store:
        ledger = ActionLedger(store, "pinned")
        ledger.claim("send_invoice", {}, key="invoice:1", pinning={"prompt_sha256": "aaa"})


def test_resume_with_matching_pinning_has_no_drift(db: str) -> None:
    import io

    from continuum.cli import main as cli_main

    _seed_pinned_run(db)
    out, err = io.StringIO(), io.StringIO()
    cli_main(
        [
            "--db",
            db,
            "--json",
            "resume",
            "pinned",
            "--pinning",
            json.dumps({"prompt_sha256": "aaa"}),
        ],
        out=out,
        err=err,
    )
    payload = json.loads(out.getvalue())
    assert payload["pinning_drift"] == []


def test_resume_with_changed_pinning_reports_the_diff(db: str) -> None:
    import io

    from continuum.cli import main as cli_main

    _seed_pinned_run(db)
    out, err = io.StringIO(), io.StringIO()
    cli_main(
        [
            "--db",
            db,
            "--json",
            "resume",
            "pinned",
            "--pinning",
            json.dumps({"prompt_sha256": "bbb", "model_id": "m9"}),
        ],
        out=out,
        err=err,
    )
    payload = json.loads(out.getvalue())
    drift = "\n".join(payload["pinning_drift"])
    assert "prompt_sha256 changed" in drift
    assert "model_id newly pinned" in drift


def test_resume_with_matching_pinning_has_no_drift_after_compaction(db: str) -> None:
    """Compaction archives the pinning-carrying prefix (issue #1126).

    The live tail then holds only the anchor marker, which carries
    ``anchored_through``/``version`` and no pinning, so a drift fold over
    ``read_events`` sees an empty record and reports every key as newly
    pinned. The fold must read full history.
    """
    import io

    from continuum.cli import main as cli_main
    from continuum.events import EventType

    _seed_pinned_run(db)

    # Guard against a vacuous pass: the pinning event must actually land in
    # the archived prefix, or the live tail would still hold it.
    with SQLiteStorage(db) as store:
        store.compact_run("pinned")
        archived_types = [e.type for e in store.read_archived_events("pinned")]
        live_types = [e.type for e in store.read_events("pinned")]
    assert EventType.ACTION_RECORDED in archived_types
    assert EventType.ACTION_RECORDED not in live_types

    out, err = io.StringIO(), io.StringIO()
    cli_main(
        [
            "--db",
            db,
            "--json",
            "resume",
            "pinned",
            "--pinning",
            json.dumps({"prompt_sha256": "aaa"}),
        ],
        out=out,
        err=err,
    )
    payload = json.loads(out.getvalue())
    assert payload["pinning_drift"] == []


def test_resume_with_changed_pinning_reports_the_diff_after_compaction(db: str) -> None:
    """Real drift still surfaces from the archive after compaction (issue #1126).

    The masked direction matters as much as the false positive: a changed
    hash would have rendered as 'newly pinned' over the live tail, and an
    unpinned key would not have rendered at all.
    """
    import io

    from continuum.cli import main as cli_main

    _seed_pinned_run(db)
    with SQLiteStorage(db) as store:
        store.compact_run("pinned")

    out, err = io.StringIO(), io.StringIO()
    cli_main(
        [
            "--db",
            db,
            "--json",
            "resume",
            "pinned",
            "--pinning",
            json.dumps({"prompt_sha256": "bbb"}),
        ],
        out=out,
        err=err,
    )
    payload = json.loads(out.getvalue())
    drift = "\n".join(payload["pinning_drift"])
    assert "prompt_sha256 changed" in drift

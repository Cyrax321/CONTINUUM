"""Compaction must preserve authority enforcement and memory history (#615, #616)."""

import io
import json

import pytest

from continuum.actions.authority import record_authority_consumed
from continuum.actions.grants import GrantDenied
from continuum.actions.ledger import ActionLedger, LedgerError, forensic_join_across_runs
from continuum.cli import main
from continuum.events import EventType
from continuum.models import Run, UnknownSideEffect
from continuum.security.hashing import stable_hash
from continuum.storage import SQLiteStorage


def cli(db, *args):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--db", str(db), *args], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def compact(db, rid="run_1"):
    code, out, err = cli(db, "compact", rid, "--force")
    assert code == 0, (out, err)
    with SQLiteStorage(str(db)) as store:
        assert store.read_archived_events(rid)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "history.db"
    with SQLiteStorage(str(path)) as store:
        store.create_run_started(Run(run_id="run_1", goal="history"))
    return path


@pytest.mark.parametrize("kind", ["grant", "authority"])
def test_consumed_authority_stays_consumed(db, kind):
    with SQLiteStorage(str(db)) as store:
        ledger = ActionLedger(store, "run_1")
        grant = {"id": "spent", "scope": "refund"} if kind == "grant" else None
        first = ledger.claim("refund", {}, key="first", grant=grant)
        ledger.complete(first.key, external_id="receipt", result={})
        if kind == "authority":
            record_authority_consumed(store, "run_1", "spent", via_action_id=first.action.action_id)
    for count in range(2):
        if count:
            compact(db)
        with SQLiteStorage(str(db)) as store:
            with pytest.raises(GrantDenied if kind == "grant" else LedgerError, match="spent"):
                ActionLedger(store, "run_1").claim(
                    "refund", {"authority_id": "spent"}, key=f"fresh-{count}", grant=grant
                )
            assert store.verify_events("run_1").ok


@pytest.mark.parametrize("kind", ["grant", "authority"])
def test_archived_live_retry_preserves_uncertainty(db, kind):
    grant = {"id": "live", "scope": "refund"} if kind == "grant" else None
    with SQLiteStorage(str(db)) as store:
        first = ActionLedger(store, "run_1").claim("refund", {}, key="live", grant=grant)
        if kind == "authority":
            record_authority_consumed(store, "run_1", "live", via_action_id=first.action.action_id)
    compact(db)
    with SQLiteStorage(str(db)) as store:
        with pytest.raises(UnknownSideEffect):
            ActionLedger(store, "run_1").claim(
                "refund", {"authority_id": "live"}, key="live", grant=grant
            )
        assert not any(e.type == EventType.GRANT_DENIED for e in store.read_all_events("run_1"))


def test_cli_gate_retains_consumed_authority(db, tmp_path):
    with SQLiteStorage(str(db)) as store:
        record_authority_consumed(store, "run_1", "spent", via_action_id="old-action")
    compact(db)
    config = tmp_path / "gate.json"
    config.write_text(json.dumps({"tools": {"refund": {"key_template": "refund:{id}"}}}))
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"tool_name": "refund", "tool_input": {"id": "new", "authority_id": "spent"}})
    )
    code, out, err = cli(
        db,
        "--json",
        "gate",
        "--run-id",
        "run_1",
        "--config",
        str(config),
        "--payload-file",
        str(payload),
    )
    assert code == 2, (out, err)
    assert "spent" in err and "consumed at seq" in err


@pytest.mark.parametrize("run_specific", [False, True])
def test_forget_and_forensics_include_archived_observations(db, run_specific):
    with SQLiteStorage(str(db)) as store:
        for rid in ["run_1", "run_2"]:
            if rid != "run_1":
                store.create_run_started(Run(run_id=rid, goal="history"))
            observation = {"content": rid}
            store.append_event(rid, EventType.PERCEPTION_OBSERVED, observation)
            ledger = ActionLedger(store, rid)
            ledger.claim(
                "mem_write",
                {},
                key=f"mem:vector:acme:{rid}",
                scoped_to_run=False,
                origin_digest=stable_hash(observation),
            )
            ledger.claim("mem_write", {}, key=f"mem:vector:other:{rid}", scoped_to_run=False)
    for rid in ["run_1", "run_2"]:
        compact(db, rid)
    with SQLiteStorage(str(db)) as store:
        for rid in ["run_1", "run_2"]:
            hits = ActionLedger(store, rid).forensic_lookup(f"acme:{rid}")
            assert len(hits) == 1
            assert hits[0]["observation_event"].payload["content"] == rid
        hits = forensic_join_across_runs(store, "acme:")
        assert len(hits) == 2
        assert all(h["observation_event"] is not None for h in hits)
    scope = ["--run-id", "run_1"] if run_specific else []
    expected = ["run_1"] if run_specific else ["run_1", "run_2"]
    for mode in [["--dry-run"], []]:
        code, out, err = cli(db, "--json", "forget", "--tenant", "acme", *scope, *mode)
        assert code == 0, (out, err)
        assert sorted(json.loads(out)["record_keys"]) == expected
    with SQLiteStorage(str(db)) as store:
        for rid in ["run_1", "run_2"]:
            assert store.verify_events(rid).ok


def test_foreign_action_scan_includes_archive(db):
    class ScanStorage(SQLiteStorage):
        supports_action_index = False

    with ScanStorage(str(db)) as store:
        first = ActionLedger(store, "run_1").claim("refund", {}, key="global", scoped_to_run=False)
        ActionLedger(store, "run_1").complete(first.key, external_id="receipt", result={})
    compact(db)
    with ScanStorage(str(db)) as store:
        store.create_run_started(Run(run_id="run_2", goal="history"))
        retry = ActionLedger(store, "run_2").claim("refund", {}, key="global", scoped_to_run=False)
        assert not retry.fresh
        assert retry.action.action_id == first.action.action_id

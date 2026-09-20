"""Memory-store gate key convention (issue #565, parent #304).

Tenancy and cross-run dedup are the two properties the key convention must
deliver: the same (store_id, tenant, record_key) triple is one identity no
matter which run wrote it, and the same record_key under a different tenant
is a different identity.

These tests pin the documented example ``mem:{store_id}:{tenant}:{record_key}``
for pgvector and Mem0 against the real ledger and gate, including the
``action_index`` foreign lookup that catches a double-write from a later run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.actions.ledger import ActionLedger, fold_action_events
from continuum.events import EventType
from continuum.gate import (
    MEMORY_KEY_PREFIX,
    GateConfigError,
    derive_memory_key,
    is_memory_key,
    is_memory_template,
    load_gate_config,
    render_key,
)
from continuum.gate import decide as gate_decide
from continuum.models import ActionStatus, Run
from continuum.storage import SQLiteStorage

CONFIG = {
    "tools": {
        "pgvector.upsert": {
            "key_template": "mem:{store_id}:{tenant}:{record_key}",
            "action_type": "mem_write",
        },
        "mem0.write": {
            "key_template": "mem:{store_id}:{tenant}:{record_key}",
            "action_type": "mem_write",
        },
        "mem_write": {
            "key_template": "mem:{store_id}:{tenant}:{record_key}",
        },
    }
}


def _fold(db: SQLiteStorage, run_id: str) -> dict[str, object]:
    return fold_action_events(db.read_events(run_id))


# --- helpers: template and derive contract --------------------------------- #


def test_is_memory_template_and_key_helpers() -> None:
    assert is_memory_template("mem:{store_id}:{tenant}:{record_key}") is True
    assert is_memory_template("invoice:{id}") is False
    assert is_memory_key("mem:pgvector:acme:rec-1") is True
    assert is_memory_key("invoice:acme:7") is False
    assert MEMORY_KEY_PREFIX == "mem:"


def test_derive_memory_key_strips_whitespace() -> None:
    rendered = derive_memory_key(
        {"store_id": " pgvector_main ", "tenant": " acme\n", "record_key": " rec-42 "},
    )
    assert rendered == "mem:pgvector_main:acme:rec-42"
    # Same triple with clean values must render identically.
    clean = derive_memory_key(
        {"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"},
    )
    assert rendered == clean


def test_render_key_for_memory_template() -> None:
    rendered = render_key(
        "mem:{store_id}:{tenant}:{record_key}",
        {"store_id": "mem0", "tenant": "acme", "record_key": "user-pref-7"},
    )
    assert rendered == "mem:mem0:acme:user-pref-7"


def test_load_gate_config_rejects_memory_template_missing_tenant(tmp_path: Path) -> None:
    bad = tmp_path / "gate.json"
    bad.write_text(
        json.dumps(
            {
                "tools": {
                    "pgvector.upsert": {
                        "key_template": "mem:{store_id}:{record_key}",
                        "action_type": "mem_write",
                    }
                }
            }
        )
    )
    with pytest.raises(GateConfigError, match="must include placeholders"):
        load_gate_config(bad)


def test_load_gate_config_accepts_documented_example(tmp_path: Path) -> None:
    example = tmp_path / "gate.json"
    example.write_text(json.dumps(CONFIG))
    loaded = load_gate_config(example)
    assert loaded is not None
    assert "pgvector.upsert" in loaded
    assert loaded["pgvector.upsert"]["key_template"] == "mem:{store_id}:{tenant}:{record_key}"


# --- ledger: cross-run dedup via action_index ------------------------------ #


def test_same_record_same_tenant_dedupes_locally(tmp_path: Path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        rendered = "mem:pgvector_main:acme:rec-42"
        first = ledger.claim("mem_write", {}, key=rendered, scoped_to_run=False)
        assert first.fresh is True
        ledger.complete(first.key, external_id="rec-42")
        folded = fold_action_events(store.read_events("run_1"))
        decision = gate_decide(
            CONFIG["tools"],
            "pgvector.upsert",
            {"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"},
            run_id="run_1",
            actions_by_key=folded,
        )
        assert decision.allow is False
        assert "already completed" in decision.reason


def test_same_record_same_tenant_dedupes_cross_run_via_index(tmp_path: Path) -> None:
    """The ledger's action_index catches a double-write from a later run.

    Run_1 completes ``mem:pgvector_main:acme:rec-42`` with a global key.
    Run_2's local log is empty, but the gate consults ``storage.foreign_action``
    for mem keys, so the duplicate is still denied. This is the property the
    memory governance guide promises.
    """
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        for rid in ("run_1", "run_2"):
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
        # Run_1 writes and completes the record globally.
        a = ActionLedger(store, "run_1")
        rendered = "mem:pgvector_main:acme:rec-42"
        first = a.claim("mem_write", {}, key=rendered, scoped_to_run=False)
        a.complete(first.key, external_id="rec-42")
        # Run_2 attempts the same triple. Its local fold is empty.
        folded_run2 = fold_action_events(store.read_events("run_2"))
        assert folded_run2 == {}
        # Without storage, the gate sees no local claim and would deny as
        # unclaimed, not as duplicate. With storage, it finds the foreign
        # completion and reports already completed --- the cross-run dedup.
        no_store = gate_decide(
            CONFIG["tools"],
            "pgvector.upsert",
            {"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"},
            run_id="run_2",
            actions_by_key=folded_run2,
        )
        assert no_store.allow is False
        assert "has no ledger claim" in no_store.reason
        with_store = gate_decide(
            CONFIG["tools"],
            "pgvector.upsert",
            {"store_id": "pgvector_main", "tenant": "acme", "record_key": "rec-42"},
            run_id="run_2",
            actions_by_key=folded_run2,
            storage=store,
        )
        assert with_store.allow is False
        assert "already completed" in with_store.reason


def test_ledger_cross_run_dedup_without_gate(tmp_path: Path) -> None:
    """Direct ledger claim with global scope dedupes without any gate.

    This is the underlying guarantee the gate builds on: the same global
    idempotency key claimed in run_2 returns fresh=False when run_1 already
    completed it, via the action_index foreign lookup.
    """
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        for rid in ("run_1", "run_2"):
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
        a = ActionLedger(store, "run_1")
        b = ActionLedger(store, "run_2")
        rendered = "mem:mem0:acme:user-pref-7"
        first = a.claim("mem_write", {}, key=rendered, scoped_to_run=False)
        a.complete(first.key, external_id="user-pref-7")
        second = b.claim("mem_write", {}, key=rendered, scoped_to_run=False)
        assert second.fresh is False
        assert second.action.external_id == "user-pref-7"
        assert second.action.status is ActionStatus.COMPLETED


def test_different_tenant_same_record_does_not_dedupe(tmp_path: Path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        first = ledger.claim(
            "mem_write", {}, key="mem:pgvector_main:acme:rec-42", scoped_to_run=False
        )
        ledger.complete(first.key, external_id="rec-42")
        folded = fold_action_events(store.read_events("run_1"))
        # Same record_key but different tenant renders a different key.
        decision = gate_decide(
            CONFIG["tools"],
            "pgvector.upsert",
            {"store_id": "pgvector_main", "tenant": "globex", "record_key": "rec-42"},
            run_id="run_1",
            actions_by_key=folded,
        )
        assert decision.allow is False
        assert "has no ledger claim" in decision.reason
        # Claiming the different-tenant key is fresh.
        second = ledger.claim(
            "mem_write", {}, key="mem:pgvector_main:globex:rec-42", scoped_to_run=False
        )
        assert second.fresh is True


def test_different_tenant_cross_run_does_not_dedupe(tmp_path: Path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        for rid in ("run_1", "run_2"):
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
        a = ActionLedger(store, "run_1")
        first = a.claim("mem_write", {}, key="mem:pgvector_main:acme:rec-42", scoped_to_run=False)
        a.complete(first.key, external_id="rec-42")
        # Run_2 writes same record_key under a different tenant: must be fresh.
        b = ActionLedger(store, "run_2")
        second = b.claim(
            "mem_write", {}, key="mem:pgvector_main:globex:rec-42", scoped_to_run=False
        )
        assert second.fresh is True
        folded2 = fold_action_events(store.read_events("run_2"))
        decision = gate_decide(
            CONFIG["tools"],
            "pgvector.upsert",
            {"store_id": "pgvector_main", "tenant": "globex", "record_key": "rec-42"},
            run_id="run_2",
            actions_by_key=folded2,
            storage=store,
        )
        # The globex key has a local STARTED claim, so it is allowed.
        assert decision.allow is True


def test_padded_tenant_still_hits_same_key(tmp_path: Path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        ledger = ActionLedger(store, "run_1")
        first = ledger.claim(
            "mem_write", {}, key="mem:pgvector_main:acme:rec-42", scoped_to_run=False
        )
        ledger.complete(first.key, external_id="rec-42")
        folded = fold_action_events(store.read_events("run_1"))
        decision = gate_decide(
            CONFIG["tools"],
            "pgvector.upsert",
            {"store_id": "pgvector_main", "tenant": " acme ", "record_key": "rec-42"},
            run_id="run_1",
            actions_by_key=folded,
        )
        assert decision.allow is False
        assert "already completed" in decision.reason


def test_gateway_derived_key_matches_gate(tmp_path: Path) -> None:
    """The gateway normalizes the same way the gate does (issue #361).

    Both seams share ``normalize_key_value``, so a padded tenant renders the
    same mem key wherever it is derived.
    """
    from continuum.gateway import render_key as gw_render

    body = {"store_id": "pgvector_main", "tenant": " acme ", "record_key": "rec-42"}
    gate_rendered = render_key("mem:{store_id}:{tenant}:{record_key}", body)
    gw_rendered = gw_render("mem:{store_id}:{tenant}:{record_key}", body)
    assert gate_rendered == gw_rendered == "mem:pgvector_main:acme:rec-42"


def test_scoped_non_memory_keys_do_not_cross_run_dedupe(tmp_path: Path) -> None:
    path = str(tmp_path / "mem.db")
    with SQLiteStorage(path) as store:
        for rid in ("run_1", "run_2"):
            store.create_run(Run(run_id=rid, goal="g"))
            store.append_event(rid, EventType.RUN_STARTED, {"goal": "g"})
        a = ActionLedger(store, "run_1")
        first = a.claim("send_invoice", {}, key="invoice:7", scoped_to_run=True)
        a.complete(first.key, external_id="INV-7")
        # Same key but run-scoped: run_2's claim is fresh, not a duplicate.
        b = ActionLedger(store, "run_2")
        second = b.claim("send_invoice", {}, key="invoice:7", scoped_to_run=True)
        assert second.fresh is True

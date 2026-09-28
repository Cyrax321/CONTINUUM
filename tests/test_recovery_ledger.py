from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace

import pytest

from continuum.concurrency import InMemoryLeaseCoordinator
from continuum.models import RecoveryContract, RecoverySafety
from continuum.recovery import (
    FileLedgerBackend,
    LedgerEntryKind,
    LedgerLockError,
    MemoryLedgerBackend,
    RecoveryLedger,
    RecoveryLedgerEntry,
)


def _contract(
    version: int = 0, status: RecoverySafety = RecoverySafety.REQUIRES_REPAIR
) -> RecoveryContract:
    return RecoveryContract(run_id="run_1", checkpoint_version=version, recovery_status=status)


@pytest.fixture
def ledger() -> Iterator[RecoveryLedger]:
    yield RecoveryLedger(MemoryLedgerBackend())


def test_append_decision_chains_from_genesis(ledger: RecoveryLedger) -> None:
    entry = ledger.append_decision("run_1", _contract(0))
    assert entry.sequence == 0
    assert entry.prev_hash == "genesis"
    assert entry.kind == LedgerEntryKind.DECISION.value
    ok, trusted = ledger.verify("run_1")
    assert ok is True
    assert trusted == 1


def test_tampering_breaks_chain(ledger: RecoveryLedger) -> None:
    ledger.append_decision("run_1", _contract(0))
    ledger.append_decision("run_1", _contract(1))

    entries = ledger.entries("run_1")
    tampered = replace(entries[0], note="hacked")
    ledger._backend.replace("run_1", [tampered, entries[1]])

    ok, broken_at = ledger.verify("run_1")
    assert ok is False
    assert broken_at == 0


def test_compact_keeps_anchors_and_recent_and_stays_verifiable(ledger: RecoveryLedger) -> None:
    ledger.append_decision("run_1", _contract(0))  # plain, old
    ledger.append_decision("run_1", _contract(1), anchor=True)  # anchor, old
    ledger.append_decision("run_1", _contract(2))  # plain, old
    ledger.append_decision("run_1", _contract(3))  # newest
    ledger.append_decision("run_1", _contract(4))  # newest

    removed = ledger.compact("run_1", keep=2, keep_anchors=True)
    assert removed == 2

    kept_sequences = {e.sequence for e in ledger.entries("run_1")}
    assert kept_sequences == {1, 3, 4}
    ok, _ = ledger.verify("run_1")
    assert ok is True


def test_append_after_compact_keeps_chain_and_clears_gate(ledger: RecoveryLedger) -> None:
    """Entries appended after a compaction must not collide with the sparse
    sequences the survivors kept: verify() must stay ok on an untampered
    ledger, and a post-compaction gate approval must clear the pending gate.
    """
    for _ in range(55):
        ledger.record_attempt("run_1")
    ledger.append_decision("run_1", _contract(), gate="required")
    for _ in range(5):
        ledger.record_attempt("run_1")

    removed = ledger.compact("run_1", keep=10)
    assert removed == 51
    ok, _ = ledger.verify("run_1")
    assert ok is True
    # Precondition: the gate-required decision survived the compaction and is
    # still pending, so the post-approval assertion below actually tests
    # clearing rather than an already-empty gate.
    assert ledger.pending_gate("run_1") is not None

    max_sequence = max(e.sequence for e in ledger.entries("run_1"))
    approved = ledger.record_gate("run_1", "approved")
    assert approved.sequence == max_sequence + 1

    sequences = [e.sequence for e in ledger.entries("run_1")]
    assert len(set(sequences)) == len(sequences), "sequences must not collide"
    ok, broken_at = ledger.verify("run_1")
    assert ok is True, f"untampered ledger reported broken at index {broken_at}"
    assert ledger.pending_gate("run_1") is None


def test_attempts_and_requires_human(ledger: RecoveryLedger) -> None:
    ledger.record_attempt("run_1")
    ledger.record_attempt("run_1")
    assert ledger.attempts("run_1") == 2
    assert ledger.requires_human("run_1", max_attempts=3) is False
    ledger.record_attempt("run_1")
    assert ledger.requires_human("run_1", max_attempts=3) is True


def test_human_gate_persists_and_clears_pending(ledger: RecoveryLedger) -> None:
    ledger.append_decision("run_1", _contract(), gate="required")
    assert ledger.pending_gate("run_1") is not None
    ledger.record_gate("run_1", "approved")
    assert ledger.pending_gate("run_1") is None


def test_reconcile_detects_version_drift(ledger: RecoveryLedger) -> None:
    ledger.append_decision("run_1", _contract(version=5))
    aligned = ledger.reconcile("run_1", SimpleNamespace(version=5))
    assert aligned.drift is False

    drifted = ledger.reconcile("run_1", SimpleNamespace(version=3))
    assert drifted.drift is True


def test_file_backend_round_trips(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = FileLedgerBackend(str(tmp_path))
    RecoveryLedger(backend).append_decision("run_1", _contract(0))
    RecoveryLedger(backend).append_decision("run_1", _contract(1))

    reopened = RecoveryLedger(backend)
    assert len(reopened.entries("run_1")) == 2
    assert reopened.verify("run_1")[0] is True


def test_file_backend_sanitizes_windows_reserved_characters(tmp_path) -> None:
    """A run id may carry characters Windows forbids in filenames (#842).

    Only the separators were replaced, so a caller-supplied id like
    ``run:1<2026>?`` produced a ledger file that could not be opened on
    Windows at all. Every forbidden character must round-trip through a
    name that opens on both platform families.
    """
    hostile = 'run:1<2026>?"*|/x\\y'
    backend = FileLedgerBackend(str(tmp_path))
    RecoveryLedger(backend).append_decision(hostile, _contract(0))

    reopened = RecoveryLedger(backend)
    assert len(reopened.entries(hostile)) == 1
    assert reopened.verify(hostile)[0] is True


def test_file_backend_sanitizes_windows_reserved_device_names(tmp_path) -> None:
    """``CON`` and friends are reserved as filenames with any extension.

    The ``ledger-`` prefix already shields the real filename, but the
    sanitizer must not become a trap the day the prefix changes (#842).
    """
    from continuum.recovery.ledger import _sanitize_run_id

    assert _sanitize_run_id("CON") == "_CON"
    assert _sanitize_run_id("nul.backup") == "_nul.backup"
    assert _sanitize_run_id("com1") == "_com1"
    # Ordinary ids, including ones that merely contain the substring, pass.
    assert _sanitize_run_id("run_1") == "run_1"
    assert _sanitize_run_id("connection") == "connection"
    assert _sanitize_run_id("a/b\\c:d") == "a_b_c_d"


def test_pending_gate_survives_prior_approval(ledger: RecoveryLedger) -> None:
    # Regression for #176: an approval for an earlier decision must not clear
    # the gate of a later gate-required decision.
    ledger.append_decision("run_1", _contract(0), gate="required")
    ledger.record_gate("run_1", "approved")
    second = ledger.append_decision("run_1", _contract(1), gate="required")

    pending = ledger.pending_gate("run_1")
    assert pending is not None
    assert pending.entry_id == second.entry_id


def test_requires_human_survives_compaction(ledger: RecoveryLedger) -> None:
    # Regression for #177: once the attempt threshold is crossed the escalation
    # marker is anchored, so compaction cannot silently reset it.
    for _ in range(3):
        ledger.record_attempt("run_1", max_attempts=3)
    assert ledger.requires_human("run_1", max_attempts=3) is True

    ledger.append_decision("run_1", _contract(0))
    ledger.compact("run_1", keep=1)
    assert ledger.attempts("run_1") == 0
    assert ledger.requires_human("run_1", max_attempts=3) is True


def test_reconcile_uses_high_water_mark(ledger: RecoveryLedger) -> None:
    # Regression for #178: a later decision with a lower checkpoint version
    # must not lower the watermark drift is measured against.
    ledger.append_decision("run_1", _contract(version=10))
    ledger.append_decision("run_1", _contract(version=6))

    report = ledger.reconcile("run_1", SimpleNamespace(version=7))
    assert report.drift is True
    assert any("v10" in detail for detail in report.details)


def test_file_backend_defers_directory_creation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Regression for #180 (constructor side effects): constructing a backend
    # must not touch the filesystem until something is written.
    target = tmp_path / "not-yet-created"
    FileLedgerBackend(str(target))
    assert not target.exists()

    RecoveryLedger(FileLedgerBackend(str(target))).append_decision("run_1", _contract(0))
    assert target.exists()


def test_append_under_cross_process_lock() -> None:
    ledger = RecoveryLedger(MemoryLedgerBackend(), lock=InMemoryLeaseCoordinator())
    ledger.append_decision("run_1", _contract())
    assert len(ledger.entries("run_1")) == 1


def test_ledger_lock_contention_raises_ledger_lock_error() -> None:
    coord = InMemoryLeaseCoordinator()
    # Lease already held by another entity
    assert coord.acquire("run_1", "other_holder") is True

    ledger = RecoveryLedger(MemoryLedgerBackend(), lock=coord)

    with pytest.raises(LedgerLockError, match=r"could not acquire ledger lock for run 'run_1'"):
        ledger.append_decision("run_1", _contract())

    with pytest.raises(LedgerLockError, match=r"could not acquire ledger lock for run 'run_1'"):
        ledger.record_attempt("run_1")

    with pytest.raises(LedgerLockError, match=r"could not acquire ledger lock for run 'run_1'"):
        ledger.record_gate("run_1", "approved")

    with pytest.raises(LedgerLockError, match=r"could not acquire ledger lock for run 'run_1'"):
        ledger.compact("run_1")

    # Once released, operations succeed
    coord.release("run_1", "other_holder")
    entry = ledger.append_decision("run_1", _contract())
    assert entry.sequence == 0
    assert ledger.record_attempt("run_1") == 1
    gate_entry = ledger.record_gate("run_1", "approved")
    assert gate_entry.gate == "approved"


def test_ledger_lock_stub_refusal_raises_ledger_lock_error() -> None:
    class ContestedLeaseStub:
        def acquire(self, run_id: str, holder_id: str, ttl: object = None) -> bool:
            return False

        def release(self, run_id: str, holder_id: str) -> None:
            pass

    ledger = RecoveryLedger(MemoryLedgerBackend(), lock=ContestedLeaseStub())  # type: ignore[arg-type]

    with pytest.raises(
        LedgerLockError, match=r"could not acquire ledger lock for run 'run_contested'"
    ):
        ledger.append_decision("run_contested", _contract())

    with pytest.raises(
        LedgerLockError, match=r"could not acquire ledger lock for run 'run_contested'"
    ):
        ledger.record_attempt("run_contested")


# --- per-dependency human gate budgets (issue #1428) ------------------------- #


def test_record_attempt_tags_the_dependency(ledger: RecoveryLedger) -> None:
    """An attempt recorded against a dependency carries it, so the per-dependency
    counter has something to count."""
    ledger.record_attempt("run_1", dependency="ext:weather-api")
    ledger.record_attempt("run_1")
    tagged = [e for e in ledger.entries("run_1") if e.kind == LedgerEntryKind.ATTEMPT.value]
    assert [e.dependency for e in tagged] == ["ext:weather-api", None]
    assert ledger.attempts("run_1") == 2
    assert ledger.attempts("run_1", dependency="ext:weather-api") == 1


def test_exhausting_one_dependency_escalates_only_it(ledger: RecoveryLedger) -> None:
    """A noisy dependency burns its own allowance, not the run's (#1428).

    Two attempts against a dependency capped at 2 escalate it, while the other
    dependency and the run as a whole are still short of their thresholds and
    keep recovering on their own.
    """
    budgets = {"dependency_budgets": {"ext:weather-api": 2}}
    for _ in range(2):
        ledger.record_attempt("run_1", dependency="ext:weather-api", dependency_budgets=budgets)

    assert ledger.attempts("run_1") == 2
    assert ledger.requires_human("run_1", dependency="ext:weather-api") is True
    # The other dependency and the run are untouched.
    assert ledger.requires_human("run_1", dependency="ext:sandbox") is False
    assert ledger.requires_human("run_1", max_attempts=3) is False
    # The escalation marker is namespaced, not the run-wide one.
    assert all(e.gate != "human_required" for e in ledger.entries("run_1"))


def test_a_dependency_budget_leaves_other_dependencies_recovering(
    ledger: RecoveryLedger,
) -> None:
    """The integration case from the issue: exhausting one dependency must not
    stop an unrelated, reliable dependency from recovering automatically."""
    budgets = {"dependency_budgets": {"ext:weather-api": 1, "ext:payments": 3}}
    for _ in range(2):
        ledger.record_attempt("run_1", dependency="ext:payments", dependency_budgets=budgets)

    # The reliable dependency is still within its own, larger allowance.
    assert ledger.attempts("run_1", dependency="ext:payments") == 2
    assert (
        ledger.requires_human("run_1", dependency="ext:payments", dependency_budgets=budgets)
        is False
    )
    # ...while the flaky one escalates on its first attempt and stays escalated.
    ledger.record_attempt("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
    assert (
        ledger.requires_human("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
        is True
    )
    assert (
        ledger.requires_human("run_1", dependency="ext:payments", dependency_budgets=budgets)
        is False
    )


def test_dependency_without_an_explicit_budget_uses_the_global_default(
    ledger: RecoveryLedger,
) -> None:
    """An unnamed dependency falls back to the global default, then to the
    caller's threshold, rather than to no limit at all."""
    for _ in range(2):
        ledger.record_attempt("run_1", dependency="ext:unnamed")
    # No registry: the caller's max_attempts is the ceiling.
    assert ledger.requires_human("run_1", dependency="ext:unnamed", max_attempts=2) is True
    assert ledger.requires_human("run_1", dependency="ext:unnamed", max_attempts=5) is False

    # A registry default governs dependencies it never named individually.
    budgets = {"default_max_attempts": 2}
    assert (
        ledger.requires_human("run_1", dependency="ext:unnamed", dependency_budgets=budgets) is True
    )
    assert (
        ledger.requires_human("run_1", dependency="ext:other", dependency_budgets=budgets) is False
    )


def test_dependency_budget_takes_precedence_over_the_global_default(
    ledger: RecoveryLedger,
) -> None:
    """An explicit dependency entry wins over the registry's global default."""
    budgets = {"default_max_attempts": 10, "dependency_budgets": {"ext:weather-api": 1}}
    ledger.record_attempt("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
    assert (
        ledger.requires_human("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
        is True
    )
    # The global default is what the un-named dependency still answers to.
    assert (
        ledger.requires_human("run_1", dependency="ext:other", dependency_budgets=budgets) is False
    )


def test_a_bare_dependency_budget_mapping_works_without_a_registry(
    ledger: RecoveryLedger,
) -> None:
    """``dependency_budgets`` may be a plain ``{dependency: limit}`` map."""
    budgets = {"ext:weather-api": 2}
    for _ in range(2):
        ledger.record_attempt("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
    assert (
        ledger.requires_human("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
        is True
    )
    # Another dependency is not in the map, so it takes the caller's threshold.
    assert (
        ledger.requires_human("run_1", dependency="ext:other", dependency_budgets=budgets) is False
    )


def test_dependency_escalation_marker_is_written_once(ledger: RecoveryLedger) -> None:
    """Attempts past a dependency's ceiling keep counting without piling up
    duplicate escalation markers."""
    budgets = {"ext:weather-api": 1}
    for _ in range(4):
        ledger.record_attempt("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
    markers = [
        e
        for e in ledger.entries("run_1")
        if e.kind == LedgerEntryKind.GATE.value and e.gate == "human_required:ext:weather-api"
    ]
    assert len(markers) == 1
    assert markers[0].anchor is True


def test_dependency_escalation_survives_compaction(ledger: RecoveryLedger) -> None:
    """The per-dependency marker is anchored, so compaction cannot reset it, and
    it never masquerades as the run-wide escalation."""
    budgets = {"ext:weather-api": 2}
    for _ in range(2):
        ledger.record_attempt("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
    assert (
        ledger.requires_human("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
        is True
    )

    ledger.append_decision("run_1", _contract(0))
    ledger.compact("run_1", keep=1)

    assert ledger.attempts("run_1", dependency="ext:weather-api") == 0
    assert (
        ledger.requires_human("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
        is True
    )
    # Escalating one dependency must not escalate the run.
    assert ledger.requires_human("run_1", max_attempts=3) is False


def test_run_wide_escalation_still_gates_every_dependency(ledger: RecoveryLedger) -> None:
    """A global ``human_required`` marker wins for every dependency too (#1428).

    Fail-closed in that direction: once the run needs a person, a dependency
    asking for itself is not a reason to let automation continue.
    """
    for _ in range(3):
        ledger.record_attempt("run_1", max_attempts=3)
    assert ledger.requires_human("run_1") is True
    assert ledger.requires_human("run_1", dependency="ext:weather-api") is True


def test_file_backend_round_trips_the_dependency_tag(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The dependency tag survives persistence, so a reopened ledger still
    counts per-dependency attempts (#1428)."""
    backend = FileLedgerBackend(str(tmp_path))
    RecoveryLedger(backend).record_attempt(
        "run_1", dependency="ext:weather-api", dependency_budgets={"ext:weather-api": 1}
    )

    reopened = RecoveryLedger(backend)
    entries = reopened.entries("run_1")
    assert [e.dependency for e in entries] == ["ext:weather-api"] * 2
    assert [e.kind for e in entries] == [
        LedgerEntryKind.ATTEMPT.value,
        LedgerEntryKind.GATE.value,
    ]
    assert reopened.attempts("run_1", dependency="ext:weather-api") == 1
    assert reopened.requires_human("run_1", dependency="ext:weather-api") is True
    # A ledger written before the field existed loads with no tag and behaves
    # exactly as before: untagged attempts count globally, not per dependency.
    assert reopened.requires_human("run_1", dependency="ext:never-attempted") is False


def test_legacy_entry_without_dependency_verifies_cleanly() -> None:
    """An entry sealed before the dependency field existed (no dependency key in
    content) still verifies its SHA-256 integrity hash."""
    from continuum.models import utcnow
    from continuum.security.hashing import stable_hash

    payload = {
        "entry_id": "leg-1",
        "run_id": "run_1",
        "sequence": 0,
        "prev_hash": "genesis",
        "kind": "attempt",
        "contract": None,
        "gate": None,
        "anchor": False,
        "created_at": utcnow().isoformat(),
        "note": "legacy attempt",
    }
    digest = stable_hash(payload)
    record = {
        "entry_id": "leg-1",
        "run_id": "run_1",
        "sequence": 0,
        "prev_hash": "genesis",
        "content_hash": digest,
        "kind": "attempt",
        "contract": None,
        "gate": None,
        "anchor": False,
        "created_at": payload["created_at"],
        "note": "legacy attempt",
    }
    entry = RecoveryLedgerEntry.from_record(record)
    assert entry.dependency is None
    assert "dependency" not in entry.content()
    assert entry.verify() is True


def test_tagged_attempts_do_not_drain_untagged_global_pool_or_set_run_wide_gate(
    ledger: RecoveryLedger,
) -> None:
    """A flaky external dependency exceeding the global max_attempts threshold
    must not set the run-wide gate or starve untagged attempts (#1428, #1459)."""
    budgets = {"dependency_budgets": {"ext:weather-api": 5}}
    for _ in range(5):
        ledger.record_attempt(
            "run_1",
            max_attempts=3,
            dependency="ext:weather-api",
            dependency_budgets=budgets,
        )

    # 5 attempts were recorded for the run, but all 5 were tagged to ext:weather-api.
    assert ledger.attempts("run_1") == 5
    assert ledger.attempts("run_1", dependency="ext:weather-api") == 5
    assert (
        ledger.requires_human("run_1", dependency="ext:weather-api", dependency_budgets=budgets)
        is True
    )

    # Untagged global attempts remain 0, so the run-wide threshold of 3 is not breached.
    assert ledger.requires_human("run_1", max_attempts=3) is False
    assert all(e.gate != "human_required" for e in ledger.entries("run_1"))


def test_multi_dependency_action_records_one_attempt_per_dependency(
    ledger: RecoveryLedger,
) -> None:
    """An action targeting multiple comma-separated dependencies records an attempt
    against each target dependency (#1459)."""
    from continuum.models import Action

    action = Action(
        action_id="act-1",
        run_id="run_1",
        action_type="sync_both",
        dep_scope="ext:weather-api, ext:sandbox",
    )
    budgets = {"dependency_budgets": {"ext:weather-api": 2, "ext:sandbox": 3}}
    ledger.record_attempt("run_1", action=action, dependency_budgets=budgets)

    assert ledger.attempts("run_1", dependency="ext:weather-api") == 1
    assert ledger.attempts("run_1", dependency="ext:sandbox") == 1
    assert ledger.attempts("run_1") == 2
    assert ledger.requires_human("run_1", action=action, dependency_budgets=budgets) is False

    # Second attempt exhausts ext:weather-api (ceiling 2), but not ext:sandbox (ceiling 3).
    ledger.record_attempt("run_1", action=action, dependency_budgets=budgets)
    assert ledger.attempts("run_1", dependency="ext:weather-api") == 2
    assert ledger.attempts("run_1", dependency="ext:sandbox") == 2
    assert ledger.requires_human("run_1", action=action, dependency_budgets=budgets) is True
    assert (
        ledger.requires_human("run_1", dependency="ext:sandbox", dependency_budgets=budgets)
        is False
    )


def test_deriving_dependencies_from_contract_and_scope(ledger: RecoveryLedger) -> None:
    """derive_recovery_dependencies resolves targets from contracts, actions, and scopes (#1459)."""
    contract = _contract(0).model_copy(
        update={
            "required_actions": ["revalidate_dependency:ext:weather-api"],
            "evidence": ["localized recovery scoped to: dataset, ext:other"],
        }
    )
    budgets = {"dependency_budgets": {"ext:weather-api": 1, "dataset": 3, "ext:other": 3}}
    ledger.record_attempt("run_1", contract=contract, dependency_budgets=budgets)

    assert ledger.attempts("run_1", dependency="ext:weather-api") == 1
    assert ledger.attempts("run_1", dependency="dataset") == 1
    assert ledger.attempts("run_1", dependency="ext:other") == 1
    # ext:weather-api reached ceiling 1, so the contract now requires human.
    assert ledger.requires_human("run_1", contract=contract, dependency_budgets=budgets) is True
    # But a scoped check on dataset alone is not exhausted.
    assert ledger.requires_human("run_1", scope=["dataset"], dependency_budgets=budgets) is False

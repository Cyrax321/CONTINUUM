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
    MemoryLedgerBackend,
    RecoveryLedger,
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

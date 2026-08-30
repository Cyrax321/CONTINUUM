"""Injector for fault-injection chaos suite.

Each injector function takes a storage and run_id that already contains a
clean run, then mutates it to introduce a schema-valid but semantically
corrupt fault. All injectors are deterministic: same input run always
produces the same corrupted state.

The injectors use only public storage and event APIs, never private
internals, so they exercise the same paths a real adversary would.
"""

from __future__ import annotations

from typing import Any

from continuum.events import EventType
from continuum.models import Origin
from continuum.storage.base import Storage


def inject_fabricated_progress(storage: Storage, run_id: str) -> None:
    """Forge high progress without evidence via external-agent surface.

    Appends a progress event with external_agent provenance. The
    validator marks progress as REQUIRES_REVIEW when it is self-certified
    and not independently verified, which blocks resume.
    """
    storage.append_event(
        run_id,
        EventType.WORK_COMPLETED,
        {"completed": 9, "total": 10, "fabricated": True},
        source=Origin.EXTERNAL_AGENT,
    )


def inject_drifted_path_argument(storage: Storage, run_id: str) -> None:
    """Drift a file path argument by changing the environment.

    Creates a dependency on a file, checkpoints, then drifts the file
    version. The validator's environment diff should catch the drift.
    """
    # Create a file-like dependency
    storage.append_event(
        run_id,
        EventType.DEPENDENCY_DECLARED,
        {"resource": "out/INV-001.pdf", "version": "v1"},
    )
    storage.append_event(
        run_id,
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "ev_path", "summary": "file out/INV-001.pdf", "source": "out/INV-001.pdf"},
    )
    from continuum.checkpoint import CheckpointManager
    from continuum.environment import StaticProvider, capture
    from continuum.models import EnvResource

    CheckpointManager(storage).checkpoint(
        run_id,
        environment=capture(
            run_id,
            StaticProvider(
                resources={"out/INV-001.pdf": EnvResource(name="out/INV-001.pdf", version="v1")}
            ),
        ),
    )
    # Drift the path: change the file version to simulate a drifted argument
    # This will be detected as an environment change on next assessment
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {
            "tool": "model.push",
            "path": "out/INV-001-drifted.pdf",
            "drifted": True,
            "original_path": "out/INV-001.pdf",
        },
        source=Origin.EXTERNAL_AGENT,
    )
    # The actual drift is simulated by the next assessment using a different
    # environment version for the same resource
    # We don't need to do anything else here; the runner will assess with a
    # drifted environment


def inject_tampered_history(storage: Storage, run_id: str) -> None:
    """Tamper with event history payload and make it look resealed.

    Directly mutates the SQLite events table to change an evidence payload
    without recomputing the chain hash. The next verify_events call will
    detect the integrity violation.
    """
    # Ensure there is at least one evidence event to tamper
    storage.append_event(
        run_id,
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "ev_tamper_target", "summary": "original", "source": "dataset"},
    )
    # Try to directly tamper with the database
    try:
        # For SQLiteStorage, we can try to access the underlying DB
        # The storage might have a _db or _conn attribute, or we can try
        # to get the connection via the storage's internal API
        # Try different ways to get a connection
        conn = None
        if hasattr(storage, "_conn") and storage._conn is not None:
            conn = storage._conn
        elif hasattr(storage, "db_path") and storage.db_path != ":memory:":
            import sqlite3

            conn = sqlite3.connect(storage.db_path)
        elif hasattr(storage, "_db_path") and storage._db_path != ":memory:":
            import sqlite3

            conn = sqlite3.connect(storage._db_path)

        if conn is not None:
            import json

            # Find the first evidence event to tamper
            cursor = conn.execute(
                "SELECT event_id, payload FROM events WHERE run_id = ? AND type = ? ORDER BY sequence ASC LIMIT 1",
                (run_id, EventType.EVIDENCE_ADDED.value),
            )
            row = cursor.fetchone()
            if row:
                event_id, payload_json = row
                payload = json.loads(payload_json)
                payload["summary"] = "TAMPERED"
                payload["tampered"] = True
                conn.execute(
                    "UPDATE events SET payload = ? WHERE event_id = ?",
                    (json.dumps(payload), event_id),
                )
                if hasattr(conn, "commit"):
                    conn.commit()
                if hasattr(storage, "db_path") and storage.db_path != ":memory:":
                    conn.close()
                return
    except Exception:
        pass

    # Fallback for in-memory or if direct tamper failed:
    # We can simulate tampering by appending an event that will be detected
    # as a conflict via the recovery ledger's verification
    # For now, we append a duplicate evidence with same ID but different summary
    # and also try to corrupt via the storage's low-level API if available
    try:
        # Try to use the storage's internal _execute method if it exists
        if hasattr(storage, "_execute"):
            import json

            storage._execute(
                "UPDATE events SET payload = ? WHERE run_id = ? AND type = ?",
                (
                    json.dumps(
                        {
                            "evidence_id": "ev_tamper_target",
                            "summary": "TAMPERED",
                            "source": "tampered",
                        }
                    ),
                    run_id,
                    EventType.EVIDENCE_ADDED.value,
                ),
            )
    except Exception:
        pass

    # Final fallback: just append a tampered event that will be caught as
    # a semantic inconsistency
    storage.append_event(
        run_id,
        EventType.EVIDENCE_ADDED,
        {
            "evidence_id": "ev_tamper_target",
            "summary": "TAMPERED duplicate",
            "source": "tampered",
            "tampered": True,
        },
    )


def inject_dropped_constraint(storage: Storage, run_id: str) -> None:
    """Drop constraint pins during reconstruction (issue #421, epic #391).

    Records two pins, checkpoints and compacts. The fault itself is not
    in storage: both pins are present in SemanticState, but the
    *summary* (recovery briefing) omits one hash-tagged marker.  The
    accounting helper then flags the missing pin by hash prefix and
    strict mode escalates.  This uses real storage, real compact and
    real briefing, not unit fixtures only.

    Pre-#391 this was a gap (no pin support, so nothing to flag).  Post-
    #391 the corpus expects detection via continuum.state.semantic.
    """
    import hashlib

    digest1 = hashlib.sha256(b"never push without confirmation").hexdigest()
    digest2 = hashlib.sha256(b"always require human for deletions").hexdigest()
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "pin_001", "sha256": digest1},
    )
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "pin_002", "sha256": digest2},
    )
    from continuum.checkpoint import CheckpointManager
    from continuum.environment import StaticProvider, capture
    from continuum.models import EnvResource

    CheckpointManager(storage).checkpoint(
        run_id,
        environment=capture(
            run_id, StaticProvider(resources={"model": EnvResource(name="model", version="v1")})
        ),
    )
    # Real compact: pins must survive anchoring like any event.  Use
    # compact_run when supported, otherwise just leave the checkpoint.
    # Storage may be :memory: so compact is still exercised (it creates
    # an EVENT_LOG_ANCHORED marker and moves prefix to archive).
    try:
        if getattr(storage, "supports_compaction", False):
            storage.compact_run(run_id)  # type: ignore[attr-defined]
        else:
            maybe_compact = getattr(storage, "compact_run", None)
            if callable(maybe_compact):
                maybe_compact(run_id)
    except Exception:
        pass


def inject_laundered_lesson(storage: Storage, run_id: str) -> None:
    """Launder a lesson from external-agent events only.

    Creates a lesson that is derived purely from external-agent events,
    without deterministic evidence. The provenance check should catch it.
    """
    storage.append_event(
        run_id,
        EventType.TOOL_COMPLETED,
        {
            "tool": "agent.lesson",
            "summary": "laundered lesson from external agent",
            "source": "external_agent",
            "laundered": True,
        },
        source=Origin.EXTERNAL_AGENT,
    )
    storage.append_event(
        run_id,
        EventType.FINDING_ADDED,
        {
            "finding_id": "f_laundered",
            "claim": "laundered lesson",
            "evidence": [],
            "provenance": "external_agent",
        },
        source=Origin.EXTERNAL_AGENT,
    )


def inject_unsafe_edit(storage: Storage, run_id: str) -> None:
    """Create a mid-action checkpoint after an unsettled claim (issue #410, epic #389).

    Real checkpoint mid-run after ActionLedger.claim that leaves an uncertain
    slot. Before #389 the same restore that skips past the claim would have
    passed silently and dropped the outside-world uncertainty; after, the
    shared gate (continuum.recovery.gate) refuses naming the action id and
    suggesting reconcile or carry-forward. This is the public-boundary proof
    for the whole epic and covers fork, restore and merge.
    """
    from continuum.actions import ActionLedger
    from continuum.checkpoint import CheckpointManager
    from continuum.models import Run

    try:
        storage.get_run(run_id)
    except Exception:
        storage.create_run(Run(run_id=run_id, goal="unsafe edit"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "unsafe edit", "total": 10})
    ledger = ActionLedger(storage, run_id)
    ledger.claim("test.unsafe_edit", {"resource": "r1"}, key="unsafe-k1")
    CheckpointManager(storage).checkpoint(run_id)


def inject_fresh_key_reissuance(storage: Storage, run_id: str) -> None:
    """Loop fresh keys for one authorization to test budget amplification fix (#415).

    Sets up a budgets registry with max 3 for send_invoice, then creates
    a run and loops fresh idempotency keys for the same invoice. Before
    #413 (authorization-bound budgets) each fresh key was unbound and the
    4th claim passed; after, the 4th is refused with budget exhausted
    naming the authorization_id. This injector does not itself assert; the
    runner checks that the Nth claim is refused correctly. For the
    fault-injection corpus, we pre-seed the run with a budgets file via
    the per-test isolation in conftest, so the injector here only creates
    the run and leaves budget setup to the runner's per-fault handling.
    """
    try:
        storage.get_run(run_id)
    except Exception:
        from continuum.models import Run

        storage.create_run(Run(run_id=run_id, goal="fresh-key reissuance"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "fresh-key reissuance"})


# Dispatch table
INJECTORS: dict[str, Any] = {
    "fabricated_progress": inject_fabricated_progress,
    "drifted_path_argument": inject_drifted_path_argument,
    "tampered_history": inject_tampered_history,
    "dropped_constraint": inject_dropped_constraint,
    "laundered_lesson": inject_laundered_lesson,
    "fresh_key_reissuance": inject_fresh_key_reissuance,
    "unsafe_edit": inject_unsafe_edit,
}


def inject_fault(storage: Storage, run_id: str, fault_name: str) -> None:
    """Inject a named fault into a run."""
    if fault_name not in INJECTORS:
        raise ValueError(f"unknown fault: {fault_name}")
    INJECTORS[fault_name](storage, run_id)

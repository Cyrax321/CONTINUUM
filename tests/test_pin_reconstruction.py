"""Reconstruction accounting per pin (#418).

Tests that the three statuses are reachable, grace window is
configurable, strict escalation works, and hash-tagged markers are the
source of truth.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from continuum.checkpoint.context import build_recovery_context
from continuum.events import EventType
from continuum.models import ConstraintPinned, Run
from continuum.state.semantic import (
    account_pins_in_context,
    check_pin_accounting,
    pin_markers_for_state,
    project,
)
from continuum.storage import SQLiteStorage


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_storage_with_pins(pin_ids: list[str]) -> tuple[SQLiteStorage, str]:
    storage = SQLiteStorage(":memory:")
    run_id = "run_1"
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    for pid in pin_ids:
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id=pid, sha256=_digest(f"text for {pid}")).model_dump(),
        )
    return storage, run_id


def test_all_three_statuses_reachable() -> None:
    storage, run_id = _make_storage_with_pins(["c1", "c2", "c3"])
    try:
        from continuum.state.semantic import project

        state = project(run_id, storage.read_events(run_id))
        assert set(state.pins.keys()) == {"c1", "c2", "c3"}

        # Build a recovery context that includes all pins (present)
        ctx = build_recovery_context(state)
        rendered = ctx.render()
        # All pins should be present
        accounting = account_pins_in_context(state, rendered)
        for pid in ["c1", "c2", "c3"]:
            assert accounting[pid]["status"] == "present", f"{pid} should be present"

        # Build a context that omits one pin (absent) by manually constructing
        # a context string without the marker for c2
        rendered_without_c2 = rendered.replace(f"[pin:c2:{_digest('text for c2')[:8]}]", "")
        accounting2 = account_pins_in_context(state, rendered_without_c2)
        assert accounting2["c1"]["status"] == "present"
        assert accounting2["c2"]["status"] == "absent"
        assert accounting2["c3"]["status"] == "present"

        # Build a truncated context (unverifiable) by simulating truncation
        truncated = rendered + "\n\n[context truncated to fit budget; omitted: ACTIVE CONSTRAINTS]"
        # With truncation, absent pins become unverifiable
        # First, make c2 absent in truncated context
        truncated_without_c2 = truncated.replace(f"[pin:c2:{_digest('text for c2')[:8]}]", "")
        accounting4 = account_pins_in_context(state, truncated_without_c2)
        assert accounting4["c2"]["status"] == "unverifiable"

    finally:
        storage.close()


def test_grace_window_configurable() -> None:
    storage, run_id = _make_storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        # Get the pin's pinned_at time
        pin = state.pins["c1"]
        # Create a context without the pin (absent)
        ctx = build_recovery_context(state)
        rendered = ctx.render()
        rendered_without = rendered.replace(f"[pin:c1:{pin.sha256[:8]}]", "")

        # Within grace: no flag
        now_within = pin.pinned_at + timedelta(seconds=10)
        accounting_within = account_pins_in_context(
            state, rendered_without, grace_seconds=60, now=now_within
        )
        assert accounting_within["c1"]["status"] == "absent"
        assert not accounting_within["c1"]["past_grace"]
        assert accounting_within["c1"]["flag"] is None

        # Past grace: flag
        now_past = pin.pinned_at + timedelta(seconds=100)
        accounting_past = account_pins_in_context(
            state, rendered_without, grace_seconds=60, now=now_past
        )
        assert accounting_past["c1"]["status"] == "absent"
        assert accounting_past["c1"]["past_grace"]
        assert accounting_past["c1"]["flag"] is not None
        assert "c1" in accounting_past["c1"]["flag"]
        assert pin.sha256[:8] in accounting_past["c1"]["flag"]

    finally:
        storage.close()


def test_strict_escalation() -> None:
    storage, run_id = _make_storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        pin = state.pins["c1"]
        ctx = build_recovery_context(state)
        rendered = ctx.render()
        rendered_without = rendered.replace(f"[pin:c1:{pin.sha256[:8]}]", "")

        now_past = pin.pinned_at + timedelta(seconds=100)

        # Non-strict: advisory only, should_escalate False
        accounting, flags, should_escalate = check_pin_accounting(
            state, rendered_without, grace_seconds=60, now=now_past, strict=False
        )
        assert flags
        assert not should_escalate
        assert accounting["c1"]["flag"] is not None

        # Strict: should escalate
        accounting2, flags2, should_escalate2 = check_pin_accounting(
            state, rendered_without, grace_seconds=60, now=now_past, strict=True
        )
        assert flags2
        assert should_escalate2

        # Within grace, even strict should not escalate
        now_within = pin.pinned_at + timedelta(seconds=10)
        accounting3, flags3, should_escalate3 = check_pin_accounting(
            state, rendered_without, grace_seconds=60, now=now_within, strict=True
        )
        assert not flags3
        assert not should_escalate3

    finally:
        storage.close()


def test_hash_tagged_markers_are_source_of_truth() -> None:
    storage, run_id = _make_storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        pin = state.pins["c1"]
        # Build a context that has the marker
        ctx = build_recovery_context(state)
        rendered = ctx.render()
        assert f"[pin:c1:{pin.sha256[:8]}]" in rendered

        # Even if someone tries to fake a summary that says the pin is present,
        # the accounting should still check the marker, not the summary
        fake_context = "ACTIVE CONSTRAINTS\n  c1 is present (trust me)"
        accounting = account_pins_in_context(state, fake_context)
        # The pin should be absent because the marker is not there, even though
        # the fake summary says it's present
        assert accounting["c1"]["status"] == "absent"

        # The correct marker must be present for it to be counted as present
        correct_context = f"ACTIVE CONSTRAINTS\n  c1:{pin.sha256[:8]} [pin:c1:{pin.sha256[:8]}]"
        accounting2 = account_pins_in_context(state, correct_context)
        assert accounting2["c1"]["status"] == "present"

    finally:
        storage.close()


def test_pin_markers_for_state() -> None:
    storage, run_id = _make_storage_with_pins(["a", "b"])
    try:
        state = project(run_id, storage.read_events(run_id))
        markers = pin_markers_for_state(state)
        assert len(markers) == 2
        for pid in ["a", "b"]:
            pin = state.pins[pid]
            expected = f"[pin:{pid}:{pin.sha256[:8]}]"
            assert expected in markers
    finally:
        storage.close()


def test_grace_window_default_advisory() -> None:
    storage, run_id = _make_storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        pin = state.pins["c1"]
        ctx = build_recovery_context(state)
        rendered = ctx.render()
        rendered_without = rendered.replace(f"[pin:c1:{pin.sha256[:8]}]", "")

        # Default grace is None (no grace), so any absent is not past grace
        accounting = account_pins_in_context(state, rendered_without)
        assert accounting["c1"]["status"] == "absent"
        assert not accounting["c1"]["past_grace"]
        assert accounting["c1"]["flag"] is None

        # With grace 0, even a tiny age is past grace
        now = pin.pinned_at + timedelta(seconds=1)
        accounting2 = account_pins_in_context(state, rendered_without, grace_seconds=0, now=now)
        assert accounting2["c1"]["past_grace"]
        assert accounting2["c1"]["flag"] is not None

    finally:
        storage.close()

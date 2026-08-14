"""Toy task: a manipulated cookie-consent banner (Extension 1 end-to-end).

Mirrors the CaMeLs paper's example: a banner whose visible label reads
"Accept" but whose DOM/accessibility attributes indicate a different action.
The before/after pair is the deliverable, not just the "after".
"""

from __future__ import annotations

from continuum.events import EventType
from continuum.models import Run
from continuum.security.provenance import PlanBranch
from continuum.security.trust_gate import resolve_branch, verify_observation
from continuum.storage.sqlite import SQLiteStorage

# True state contradicts the spoofed visible label.
DOM_SNAPSHOT = 'aria-label="Decline all cookies"'
SPOOFED_CLAIM = "Accept"


def _observe(store=None, run_id=None):
    return verify_observation(
        SPOOFED_CLAIM,
        "screenshot_hash",
        dom_snapshot=DOM_SNAPSHOT,
        storage=store,
        run_id=run_id,
        # DOM check: does the claim text actually appear in the tree? (False here)
        dom_check=lambda claim, dom: claim in dom,
        # Independent second model, re-asked from the screenshot, also fooled by
        # the visible label (True here). The two disagree -> contested.
        consensus_check=lambda claim, h: True,
    )


def _branch():
    return PlanBranch(
        branch_id="consent",
        risk_tier="high",  # type: ignore[arg-type]
        action_intent="accept_cookie_consent",
        depends_on_observation=True,
    )


def _harness_execute(branch, gate=None):
    """A harness only executes a branch when the gate does not block it."""
    if gate is not None and gate.requires_review:
        return "blocked"
    return "executed"


def test_after_gate_catches_the_manipulated_branch() -> None:
    obs = _observe()
    # The two checks disagree, so the observation is contested.
    assert obs.trust_level == "contested"

    gate = resolve_branch(_branch(), obs)
    assert gate.requires_review is True

    # A harness must not execute a branch the gate has flagged.
    assert _harness_execute(_branch(), gate) == "blocked"


def test_recorded_trace_shows_flagged_event() -> None:
    store = SQLiteStorage(":memory:")
    store.create_run(Run(run_id="r1", goal="g"))

    obs = _observe(store=store, run_id="r1")
    gate = resolve_branch(_branch(), obs, storage=store, run_id="r1")

    types = [e.type for e in store.read_events("r1")]
    assert EventType.PERCEPTION_OBSERVED in types
    assert EventType.BRANCH_RESOLVED in types
    assert gate.event.payload["requires_review"] is True


def test_before_gate_disabled_attack_succeeds_silently() -> None:
    # Control: with the gate disabled, the harness never consults it and
    # executes the branch directly. The manipulated perception steers it with
    # no flag and no ledger entry.
    branch = _branch()
    assert _harness_execute(branch) == "executed"

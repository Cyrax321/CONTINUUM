"""Deferred review queue and priority batching for low-risk confirmations (issue #1410).

When an agent executes routine operations that require human confirmation,
interrupting an operator for each step stalls execution and degrades review
quality (reviewer fatigue, arXiv:2606.08919, arXiv:2606.22721). The counter-measure
is to defer and batch low-risk confirmations behind an escalation policy,
ranking queue items by blast radius, pending age, and dependency chain depth,
while surfacing immediate blockers without delay.

Queue mutations record durable events (APPROVAL_REQUESTED, APPROVAL_GRANTED,
APPROVAL_REVOKED), keeping state reconstructible by replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from continuum.events import Event, EventType
from continuum.models import Origin, utcnow
from continuum.recovery.escalation import (
    ActionRisk,
    evaluate_action_risk,
    load_escalation_policy,
)
from continuum.security.hashing import make_id
from continuum.storage import Storage

__all__ = [
    "ReviewItem",
    "ReviewQueue",
]


@dataclass
class ReviewItem:
    """A tracked confirmation item in the review queue.

    An item is either an immediate blocker (score >= blast_radius_threshold)
    or parked in the background for deferred batch review.
    """

    review_id: str
    run_id: str
    action_type: str
    arguments: dict[str, Any] = field(default_factory=dict)
    risk_score: float = 0.0
    immediate: bool = False
    parked: bool = False
    dependency: str | None = None
    dependency_depth: int = 0
    created_at: datetime = field(default_factory=utcnow)
    status: str = "pending"
    approved_by: str | None = None
    approved_at: datetime | None = None
    batch_id: str | None = None

    @property
    def is_pending(self) -> bool:
        """True when the item is still awaiting a resolution."""
        return self.status == "pending"

    @property
    def is_immediate(self) -> bool:
        """True when the item is an immediate blocker requiring prompt attention."""
        return self.immediate and self.is_pending

    @property
    def is_parked(self) -> bool:
        """True when the item is parked in the deferred review queue."""
        return self.parked and self.is_pending

    def to_dict(self) -> dict[str, Any]:
        """Serialize the review item to a JSON-compatible dictionary."""
        return {
            "review_id": self.review_id,
            "run_id": self.run_id,
            "action_type": self.action_type,
            "arguments": dict(self.arguments),
            "risk_score": self.risk_score,
            "immediate": self.immediate,
            "parked": self.parked,
            "dependency": self.dependency,
            "dependency_depth": self.dependency_depth,
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else str(self.created_at),
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat()
            if isinstance(self.approved_at, datetime)
            else (str(self.approved_at) if self.approved_at else None),
            "batch_id": self.batch_id,
        }


class ReviewQueue:
    """Manages the deferred review queue and priority batching for a run.

    State is fully derived from durable events stored in the run log.
    Pending items are priority-sorted with immediate blockers first,
    followed by risk score descending, dependency depth descending,
    and oldest pending age first.
    """

    def __init__(
        self,
        storage: Storage,
        policy: Mapping[str, Any] | None = None,
    ) -> None:
        self.storage = storage
        if policy is not None:
            self.policy = dict(policy)
        else:
            self.policy = load_escalation_policy()

    def enqueue(
        self,
        run_id: str,
        action_type: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        dependency: str | None = None,
        dependency_depth: int = 0,
        batch_id: str | None = None,
    ) -> ReviewItem:
        """Score an action and enqueue it for human confirmation.

        Actions clearing the policy blast radius threshold are flagged
        immediate blockers. All other actions are parked for deferred review.
        """
        args_dict = dict(arguments or {})
        risk: ActionRisk = evaluate_action_risk(action_type, args_dict, self.policy)
        review_id = make_id("rev")
        immediate = risk.immediate
        parked = not immediate

        assigned_batch = batch_id or (f"batch_{run_id}" if parked else None)

        payload: dict[str, Any] = {
            "approval_id": review_id,
            "review_id": review_id,
            "subject": f"review:{action_type}",
            "action_type": action_type,
            "arguments": args_dict,
            "risk_score": risk.score,
            "immediate": immediate,
            "parked": parked,
            "dependency": dependency,
            "dependency_depth": int(dependency_depth),
            "batch_id": assigned_batch,
            "status": "pending",
        }

        event = self.storage.append_event(
            run_id,
            EventType.APPROVAL_REQUESTED,
            payload,
            source=Origin.DETERMINISTIC,
        )

        return ReviewItem(
            review_id=review_id,
            run_id=run_id,
            action_type=action_type,
            arguments=args_dict,
            risk_score=risk.score,
            immediate=immediate,
            parked=parked,
            dependency=dependency,
            dependency_depth=int(dependency_depth),
            created_at=event.timestamp,
            status="pending",
            batch_id=assigned_batch,
        )

    def list_all(self, run_id: str) -> list[ReviewItem]:
        """Reconstruct all review items recorded for a run across history."""
        events: Sequence[Event] = self.storage.read_all_events(run_id)
        items: dict[str, ReviewItem] = {}

        for event in events:
            payload = event.payload or {}
            if event.type is EventType.APPROVAL_REQUESTED:
                review_id = str(payload.get("review_id") or payload.get("approval_id") or "")
                if not review_id:
                    continue
                action_type = str(payload.get("action_type") or payload.get("subject") or "unknown")
                raw_args = payload.get("arguments")
                arguments = dict(raw_args) if isinstance(raw_args, Mapping) else {}
                risk_score = float(payload.get("risk_score", 0.0))
                immediate = bool(payload.get("immediate", False))
                parked = bool(payload.get("parked", not immediate))
                dependency = payload.get("dependency")
                dep_depth = int(payload.get("dependency_depth", 0))
                batch_id = payload.get("batch_id")

                items[review_id] = ReviewItem(
                    review_id=review_id,
                    run_id=run_id,
                    action_type=action_type,
                    arguments=arguments,
                    risk_score=risk_score,
                    immediate=immediate,
                    parked=parked,
                    dependency=str(dependency) if dependency is not None else None,
                    dependency_depth=dep_depth,
                    created_at=event.timestamp,
                    status="pending",
                    batch_id=str(batch_id) if batch_id is not None else None,
                )
            elif event.type is EventType.APPROVAL_GRANTED:
                review_id = str(payload.get("review_id") or payload.get("approval_id") or "")
                if review_id in items:
                    target = items[review_id]
                    target.status = "approved"
                    target.approved_by = str(payload.get("granted_by") or "operator")
                    target.approved_at = event.timestamp
            elif event.type is EventType.APPROVAL_REVOKED:
                review_id = str(payload.get("review_id") or payload.get("approval_id") or "")
                if review_id in items:
                    target = items[review_id]
                    target.status = "revoked"

        return list(items.values())

    def list_pending(self, run_id: str) -> list[ReviewItem]:
        """Return all pending review items, priority-sorted.

        Immediate blockers come first. Within each group, items are sorted
        by consequence/risk score descending, dependency chain depth descending,
        and pending age descending (oldest first).
        """
        all_items = self.list_all(run_id)
        pending = [it for it in all_items if it.is_pending]

        def priority_key(item: ReviewItem) -> tuple[int, float, int, datetime]:
            # Immediate blockers rank ahead of parked background reviews
            group = 0 if item.immediate else 1
            # Higher risk first -> negate risk_score
            risk = -item.risk_score
            # Deeper dependency depth first -> negate depth
            depth = -item.dependency_depth
            # Oldest created_at first
            age = item.created_at
            return (group, risk, depth, age)

        pending.sort(key=priority_key)
        return pending

    def list_parked(self, run_id: str) -> list[ReviewItem]:
        """Return pending parked review items (deferred low-risk confirmations)."""
        return [it for it in self.list_pending(run_id) if it.is_parked]

    def list_immediate(self, run_id: str) -> list[ReviewItem]:
        """Return pending immediate blocker items."""
        return [it for it in self.list_pending(run_id) if it.is_immediate]

    def get_item(self, run_id: str, review_id: str) -> ReviewItem | None:
        """Fetch a specific review item by review_id, or None if not found."""
        for it in self.list_all(run_id):
            if it.review_id == review_id:
                return it
        return None

    def approve(
        self,
        run_id: str,
        review_id: str,
        reviewer: str = "operator",
    ) -> ReviewItem:
        """Approve an individual review item and record the durable event."""
        item = self.get_item(run_id, review_id)
        if item is None:
            raise KeyError(f"no review item with id {review_id!r} in run {run_id!r}")
        if not item.is_pending:
            raise ValueError(f"review item {review_id!r} is already {item.status}")

        payload: dict[str, Any] = {
            "approval_id": review_id,
            "review_id": review_id,
            "granted_by": reviewer,
            "reason": "approved via review queue",
        }

        event = self.storage.append_event(
            run_id,
            EventType.APPROVAL_GRANTED,
            payload,
            source=Origin.HUMAN,
        )

        item.status = "approved"
        item.approved_by = reviewer
        item.approved_at = event.timestamp
        return item

    def approve_low_risk(
        self,
        run_id: str,
        max_risk: float | None = None,
        reviewer: str = "operator",
    ) -> list[ReviewItem]:
        """Bulk-approve all pending items at or below the risk threshold.

        If max_risk is not specified, the policy blast_radius_threshold is used
        and immediate blockers are never bulk-approved. Immediate blockers
        always require individual review even if a custom threshold is passed.
        """
        threshold = (
            max_risk
            if max_risk is not None
            else float(self.policy.get("blast_radius_threshold", 0.8))
        )
        pending = self.list_pending(run_id)
        batch_id = make_id("batch")

        approved: list[ReviewItem] = []
        for item in pending:
            # Immediate blockers always require individual review. A caller
            # passing a custom threshold must not be able to widen the batch
            # over the blast radius threshold and clear high-risk items nobody
            # looked at, so the guard is unconditional rather than only in the
            # default-threshold path (issue #1410 review).
            if item.immediate:
                continue
            if item.risk_score <= threshold:
                payload: dict[str, Any] = {
                    "approval_id": item.review_id,
                    "review_id": item.review_id,
                    "granted_by": reviewer,
                    "reason": f"bulk-approved low-risk (threshold {threshold})",
                    "batch_id": batch_id,
                }
                event = self.storage.append_event(
                    run_id,
                    EventType.APPROVAL_GRANTED,
                    payload,
                    source=Origin.HUMAN,
                )
                item.status = "approved"
                item.approved_by = reviewer
                item.approved_at = event.timestamp
                approved.append(item)

        return approved

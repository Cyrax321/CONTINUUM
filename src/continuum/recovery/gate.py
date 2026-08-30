"""Shared precondition gate for fork, restore and merge (issues #389, #407, #408).

This module is the single entry point that enforces safety preconditions
before any execution edit executes. It reuses the pure derivation from
``preconditions.py`` (#406) and the refusal and lineage helpers first
introduced for the fork gate (#407).

Per-edit-type semantics
-----------------------
All three edit types derive the same three sets over the half-open range
``(anchor_sequence, candidate_sequence]``:

* ``unsettled_authorizations``: approvals granted inside the span and not
  revoked through the candidate.
* ``uncertain_slots``: ledger slots opened inside the span still holding an
  unresolved outcome at the candidate.
* ``depended_results``: completed actions whose recorded outcome a surviving
  plan step still references.

The first and third sets mean the same thing for every edit. The third set,
``depended_results``, has one intentional per-edit difference:

* **Fork and merge** branch history. A result completed inside the span that
  a later step *inside the same span* still references would be stranded in
  the child (fork) or in the merged view (merge) if the edit proceeded, so the
  full derived set blocks those edits. This is the behaviour first shipped for
  fork (#407).

* **Restore** reactivates history. It discards ``(anchor, head]`` and replays
  from the anchor checkpoint. A result completed inside the span that is only
  referenced inside the same span is discarded together with its dependent,
  so the pair is not stranded and will be recomputed on replay. Restore
  therefore keeps only the subset whose dependent survives the edit, i.e.
  where the key or action id is still referenced by the *surviving prefix*
  (events at or before the anchor). A result that the reactivated history at
  the anchor still requires must not be lost to the discard.

Unsettled authorizations and uncertain slots are identical for every edit
type, which is what lets a single parametrised suite cover all three edits
symmetrically. Refusal shape and lineage stamping are also identical across
edit types, only the payload's ``edit_type`` and anchor name change.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from continuum.events import EventType
from continuum.models import Origin
from continuum.storage.base import Storage

__all__ = [
    "EditPreconditionError",
    "ForkPreconditionError",
    "EditType",
    "check_preconditions",
    "check_merge_preconditions",
    "enforce",
    "render_preserved_summary",
    "render_refusal_text",
    "summary_payload",
]

EditType = Literal["fork", "restore", "merge"]


class EditPreconditionError(ValueError):
    """Edit refused because preconditions in the span are unaccounted for."""

    def __init__(
        self,
        message: str,
        *,
        edit_type: EditType,
        derivation: Any,
        unaccounted: Any,
        rationale: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.edit_type = edit_type
        self.derivation = derivation
        self.unaccounted = unaccounted
        self.rationale = rationale


class ForkPreconditionError(EditPreconditionError):
    """Alias for :class:`EditPreconditionError` when ``edit_type`` is ``fork``."""

    def __init__(
        self,
        message: str,
        *,
        derivation: Any,
        unaccounted: Any,
        rationale: dict[str, Any],
        edit_type: EditType = "fork",
    ) -> None:
        super().__init__(
            message,
            edit_type=edit_type,
            derivation=derivation,
            unaccounted=unaccounted,
            rationale=rationale,
        )


def summary_payload(derivation: Any) -> dict[str, Any]:
    """JSON-native summary of a derivation for the lineage event."""
    return {
        "unsettled_authorizations": sorted(
            [
                {
                    "approval_id": item.approval_id,
                    "subject": item.subject,
                    "sequence": item.sequence,
                }
                for item in derivation.unsettled_authorizations
            ],
            key=lambda d: d["sequence"],
        ),
        "depended_results": sorted(
            [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "sequence": item.sequence,
                }
                for item in derivation.depended_results
            ],
            key=lambda d: d["sequence"],
        ),
        "uncertain_slots": sorted(
            [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "status": item.status,
                    "sequence": item.sequence,
                }
                for item in derivation.uncertain_slots
            ],
            key=lambda d: d["sequence"],
        ),
    }


def _is_carried(candidates: Collection[str], carry_set: set[str]) -> bool:
    """True when any candidate identifier is explicitly carried."""
    return any(c in carry_set for c in candidates)


def _unaccounted_sets(
    derivation: Any,
    carry_set: set[str],
) -> Any:
    """Derivation subset that remains unaccounted after carry_forward."""
    from continuum.recovery.preconditions import DerivationResult

    unsettled = frozenset(
        item
        for item in derivation.unsettled_authorizations
        if not _is_carried({item.approval_id, str(item.sequence)}, carry_set)
    )
    depended = frozenset(
        item
        for item in derivation.depended_results
        if not _is_carried({item.key, item.action_id, str(item.sequence)}, carry_set)
    )
    uncertain = frozenset(
        item
        for item in derivation.uncertain_slots
        if not _is_carried({item.key, item.action_id, str(item.sequence)}, carry_set)
    )
    return DerivationResult(
        unsettled_authorizations=unsettled,
        depended_results=depended,
        uncertain_slots=uncertain,
    )


def _payload_strings_local(payload: Any) -> frozenset[str]:
    """Every string value reachable in a JSON-native event payload."""
    from collections.abc import Mapping

    found: set[str] = set()
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            found.add(current)
    return frozenset(found)


def _surviving_strings(storage: Storage, run_id: str, anchor: int) -> frozenset[str]:
    """All string payload values in the surviving prefix ``[1, anchor]``."""
    survivors: set[str] = set()
    from heapq import merge

    stream = merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda e: e.sequence,
    )
    for event in stream:
        if event.sequence > anchor:
            break
        survivors.update(_payload_strings_local(event.payload))
    return frozenset(survivors)


def _filtered_depended_for_edit(
    derivation: Any,
    storage: Storage,
    run_id: str,
    anchor: int,
    edit_type: EditType,
) -> Any:
    """Apply per-edit-type filtering to depended_results."""
    if edit_type != "restore":
        return derivation.depended_results
    survivors = _surviving_strings(storage, run_id, anchor)
    if not survivors:
        return frozenset()
    from heapq import merge

    from continuum.events import EventType
    from continuum.models import Action, ActionStatus

    head = storage.last_sequence(run_id)
    completions: dict[str, Any] = {}
    stream = merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda e: e.sequence,
    )
    for event in stream:
        if event.sequence <= anchor:
            continue
        if event.sequence > head:
            break
        if event.type not in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            continue
        raw_key = event.payload.get("key")
        if not raw_key:
            continue
        try:
            action = Action.model_validate(event.payload["action"])
        except Exception:
            continue
        if action.status is not ActionStatus.COMPLETED:
            continue
        from continuum.recovery.preconditions import DependedResult

        completions[str(raw_key)] = DependedResult(
            key=str(raw_key),
            action_id=action.action_id,
            action_type=action.action_type,
            sequence=event.sequence,
        )
    restored_set = frozenset(
        item for key, item in completions.items() if key in survivors or item.action_id in survivors
    )
    derived_surviving = frozenset(
        item
        for item in derivation.depended_results
        if item.key in survivors or item.action_id in survivors
    )
    return restored_set | derived_surviving


def _derive_single(
    storage: Storage,
    run_id: str,
    anchor: int,
    *,
    candidate: int | None = None,
    edit_type: EditType = "fork",
) -> Any:
    """Derive a single-run DerivationResult with per-edit filtering applied."""
    from continuum.recovery.preconditions import DerivationResult, EditPoint, derive

    head = candidate if candidate is not None else storage.last_sequence(run_id)
    if anchor > head:
        head = anchor
    point = EditPoint(
        run_id=run_id,
        anchor_sequence=anchor,
        candidate_sequence=head,
    )
    raw_derivation = derive(storage, point)
    filtered_depended = _filtered_depended_for_edit(
        raw_derivation, storage, run_id, anchor, edit_type
    )
    return DerivationResult(
        unsettled_authorizations=raw_derivation.unsettled_authorizations,
        depended_results=filtered_depended,
        uncertain_slots=raw_derivation.uncertain_slots,
    )


def _all_strings_up_to(storage: Storage, run_id: str, head: int) -> frozenset[str]:
    """All string payload values in ``[1, head]`` for cross-run checks."""
    strings: set[str] = set()
    from heapq import merge

    stream = merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda e: e.sequence,
    )
    for event in stream:
        if event.sequence > head:
            break
        strings.update(_payload_strings_local(event.payload))
    return frozenset(strings)


def _collect_completions(storage: Storage, run_id: str, anchor: int, head: int) -> dict[str, Any]:
    """Completed actions inside ``(anchor, head]`` keyed by ledger key."""
    from heapq import merge

    from continuum.events import EventType
    from continuum.models import Action, ActionStatus

    completions: dict[str, Any] = {}
    stream = merge(
        storage.read_archived_events(run_id),
        storage.read_events(run_id),
        key=lambda e: e.sequence,
    )
    for event in stream:
        if event.sequence <= anchor:
            continue
        if event.sequence > head:
            break
        if event.type not in (
            EventType.ACTION_RECORDED,
            EventType.ACTION_RECONCILED,
            EventType.ACTION_COMPENSATED,
        ):
            continue
        raw_key = event.payload.get("key")
        if not raw_key:
            continue
        try:
            action = Action.model_validate(event.payload["action"])
        except Exception:
            continue
        if action.status is not ActionStatus.COMPLETED:
            continue
        from continuum.recovery.preconditions import DependedResult

        completions[str(raw_key)] = DependedResult(
            key=str(raw_key),
            action_id=action.action_id,
            action_type=action.action_type,
            sequence=event.sequence,
        )
    return completions


def _cross_depended_for_merge(
    storage: Storage,
    target_run_id: str,
    target_anchor: int,
    target_head: int,
    source_run_id: str,
    source_anchor: int,
    source_head: int,
) -> frozenset[Any]:
    """Completions on one side referenced by the other side's log."""
    cross: set[Any] = set()
    completions_source = _collect_completions(storage, source_run_id, source_anchor, source_head)
    if completions_source:
        target_strings = _all_strings_up_to(storage, target_run_id, target_head)
        for key, item in completions_source.items():
            if key in target_strings or item.action_id in target_strings:
                cross.add(item)
    completions_target = _collect_completions(storage, target_run_id, target_anchor, target_head)
    if completions_target:
        source_strings = _all_strings_up_to(storage, source_run_id, source_head)
        for key, item in completions_target.items():
            if key in source_strings or item.action_id in source_strings:
                cross.add(item)
    return frozenset(cross)


def check_merge_preconditions(
    storage: Storage,
    target_run_id: str,
    target_anchor: int,
    *,
    source_run_id: str | None = None,
    source_anchor: int | None = None,
    carry_forward: Collection[str] | None = None,
) -> tuple[Any, set[str], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Derive and enforce preconditions for both sides of a merge.

    Target span is ``(target_anchor, target_head]`` where ``target_head`` is
    ``last_sequence(target_run_id)``. Source span is
    ``(source_anchor, source_head]`` where ``source_anchor`` is the explicit
    ancestor or, when None, ``storage.latest_version(source_run_id).source_sequence``
    (fallback 0). If ``source_run_id`` is None only the target side is checked
    for backward compatibility.

    Returns ``(union_derivation, carry_set, union_summary, target_summary, source_summary)``
    where the union blocks if either side has unaccounted items. Raises
    :class:`EditPreconditionError` with rationale naming both sides' sequences.
    """
    from continuum.recovery.preconditions import DerivationResult

    target_head = storage.last_sequence(target_run_id)
    if target_anchor > target_head:
        target_head = target_anchor
    target_derivation = _derive_single(
        storage, target_run_id, target_anchor, candidate=target_head, edit_type="merge"
    )
    target_summary = summary_payload(target_derivation)

    if source_run_id is None:
        carry_set = {str(x) for x in (carry_forward or ())}
        unaccounted = _unaccounted_sets(target_derivation, carry_set)
        if (
            unaccounted.unsettled_authorizations
            or unaccounted.depended_results
            or unaccounted.uncertain_slots
        ):
            check_preconditions(
                storage,
                target_run_id,
                target_anchor,
                candidate=target_head,
                edit_type="merge",
                carry_forward=carry_forward,
            )
            raise AssertionError("check_preconditions should have raised")
        return target_derivation, carry_set, target_summary, target_summary, None

    storage.get_run(source_run_id)
    if source_anchor is None:
        latest = storage.latest_version(source_run_id)
        source_anchor_val = latest.source_sequence if latest is not None else 0
    else:
        source_anchor_val = int(source_anchor)
    source_head = storage.last_sequence(source_run_id)
    if source_anchor_val > source_head:
        source_head = source_anchor_val
    source_derivation = _derive_single(
        storage, source_run_id, source_anchor_val, candidate=source_head, edit_type="merge"
    )
    source_summary = summary_payload(source_derivation)

    cross = _cross_depended_for_merge(
        storage,
        target_run_id,
        target_anchor,
        target_head,
        source_run_id,
        source_anchor_val,
        source_head,
    )

    union_depended = frozenset(
        set(target_derivation.depended_results)
        | set(source_derivation.depended_results)
        | set(cross)
    )
    union = DerivationResult(
        unsettled_authorizations=frozenset(
            set(target_derivation.unsettled_authorizations)
            | set(source_derivation.unsettled_authorizations)
        ),
        depended_results=union_depended,
        uncertain_slots=frozenset(
            set(target_derivation.uncertain_slots) | set(source_derivation.uncertain_slots)
        ),
    )
    carry_set = {str(x) for x in (carry_forward or ())}
    unaccounted = _unaccounted_sets(union, carry_set)

    if (
        unaccounted.unsettled_authorizations
        or unaccounted.depended_results
        or unaccounted.uncertain_slots
    ):
        parts: list[str] = []
        rationale: dict[str, Any] = {
            "edit_type": "merge",
            "anchor_sequence": target_anchor,
            "candidate_sequence": target_head,
            "divergence_sequence": target_anchor,
            "target_anchor_sequence": target_anchor,
            "target_candidate_sequence": target_head,
            "source_run_id": source_run_id,
            "source_anchor_sequence": source_anchor_val,
            "source_candidate_sequence": source_head,
            "unsettled_authorizations": [
                {
                    "approval_id": item.approval_id,
                    "sequence": item.sequence,
                    "subject": item.subject,
                }
                for item in sorted(unaccounted.unsettled_authorizations, key=lambda x: x.sequence)
            ],
            "depended_results": [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "sequence": item.sequence,
                }
                for item in sorted(unaccounted.depended_results, key=lambda x: x.sequence)
            ],
            "uncertain_slots": [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "status": item.status,
                    "sequence": item.sequence,
                }
                for item in sorted(unaccounted.uncertain_slots, key=lambda x: x.sequence)
            ],
            "carry_forward": sorted(carry_set),
            "target_preconditions": target_summary,
            "source_preconditions": source_summary,
        }
        if unaccounted.unsettled_authorizations:
            ids = ", ".join(
                f"{item.approval_id} at sequence {item.sequence}"
                for item in sorted(unaccounted.unsettled_authorizations, key=lambda x: x.sequence)
            )
            parts.append(
                f"{len(unaccounted.unsettled_authorizations)} unsettled authorization(s): {ids}"
            )
        if unaccounted.depended_results:
            ids = ", ".join(
                f"{item.action_id} (key {item.key}) at sequence {item.sequence}"
                for item in sorted(unaccounted.depended_results, key=lambda x: x.sequence)
            )
            parts.append(
                f"{len(unaccounted.depended_results)} depended result(s) would be stranded: {ids}"
            )
        if unaccounted.uncertain_slots:
            ids = ", ".join(
                f"{item.action_id} (key {item.key}, status {item.status}) at sequence {item.sequence}"
                for item in sorted(unaccounted.uncertain_slots, key=lambda x: x.sequence)
            )
            parts.append(f"{len(unaccounted.uncertain_slots)} uncertain slot(s) still open: {ids}")
        message = (
            "merge refused: preconditions in target (anchor "
            f"{target_anchor}, head {target_head}] and source (anchor "
            f"{source_anchor_val}, head {source_head}] are unaccounted for: "
            + "; ".join(parts)
            + ". Pass carry_forward with the identifiers you intend to carry, or reconcile first."
        )
        raise EditPreconditionError(
            message,
            edit_type="merge",
            derivation=union,
            unaccounted=unaccounted,
            rationale=rationale,
        )
    union_summary = summary_payload(union)
    return union, carry_set, union_summary, target_summary, source_summary


def check_preconditions(
    storage: Storage,
    run_id: str,
    anchor: int,
    *,
    candidate: int | None = None,
    edit_type: EditType = "fork",
    carry_forward: Collection[str] | None = None,
) -> tuple[Any, set[str], dict[str, Any]]:
    """Derive preconditions for ``(anchor, candidate]`` and raise if blocked."""
    from continuum.recovery.preconditions import DerivationResult, EditPoint, derive

    head = candidate if candidate is not None else storage.last_sequence(run_id)
    if anchor > head:
        head = anchor
    point = EditPoint(
        run_id=run_id,
        anchor_sequence=anchor,
        candidate_sequence=head,
    )
    raw_derivation = derive(storage, point)
    filtered_depended = _filtered_depended_for_edit(
        raw_derivation, storage, run_id, anchor, edit_type
    )
    derivation = DerivationResult(
        unsettled_authorizations=raw_derivation.unsettled_authorizations,
        depended_results=filtered_depended,
        uncertain_slots=raw_derivation.uncertain_slots,
    )
    carry_set = {str(x) for x in (carry_forward or ())}
    unaccounted = _unaccounted_sets(derivation, carry_set)

    if (
        unaccounted.unsettled_authorizations
        or unaccounted.depended_results
        or unaccounted.uncertain_slots
    ):
        anchor_key = {
            "fork": "divergence_sequence",
            "restore": "anchor_sequence",
            "merge": "anchor_sequence",
        }[edit_type]
        parts: list[str] = []
        rationale: dict[str, Any] = {
            anchor_key: anchor,
            "candidate_sequence": head,
            "edit_type": edit_type,
            "divergence_sequence": anchor,
            "anchor_sequence": anchor,
            "unsettled_authorizations": [
                {
                    "approval_id": item.approval_id,
                    "sequence": item.sequence,
                    "subject": item.subject,
                }
                for item in sorted(unaccounted.unsettled_authorizations, key=lambda x: x.sequence)
            ],
            "depended_results": [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "sequence": item.sequence,
                }
                for item in sorted(unaccounted.depended_results, key=lambda x: x.sequence)
            ],
            "uncertain_slots": [
                {
                    "key": item.key,
                    "action_id": item.action_id,
                    "action_type": item.action_type,
                    "status": item.status,
                    "sequence": item.sequence,
                }
                for item in sorted(unaccounted.uncertain_slots, key=lambda x: x.sequence)
            ],
            "carry_forward": sorted(carry_set),
        }
        if unaccounted.unsettled_authorizations:
            ids = ", ".join(
                f"{item.approval_id} at sequence {item.sequence}"
                for item in sorted(unaccounted.unsettled_authorizations, key=lambda x: x.sequence)
            )
            parts.append(
                f"{len(unaccounted.unsettled_authorizations)} unsettled authorization(s): {ids}"
            )
        if unaccounted.depended_results:
            ids = ", ".join(
                f"{item.action_id} (key {item.key}) at sequence {item.sequence}"
                for item in sorted(unaccounted.depended_results, key=lambda x: x.sequence)
            )
            parts.append(
                f"{len(unaccounted.depended_results)} depended result(s) would be stranded: {ids}"
            )
        if unaccounted.uncertain_slots:
            ids = ", ".join(
                f"{item.action_id} (key {item.key}, status {item.status}) at sequence {item.sequence}"
                for item in sorted(unaccounted.uncertain_slots, key=lambda x: x.sequence)
            )
            parts.append(f"{len(unaccounted.uncertain_slots)} uncertain slot(s) still open: {ids}")
        verb = {"fork": "fork", "restore": "restore", "merge": "merge"}[edit_type]
        message = (
            f"{verb} refused: preconditions in (anchor, head] are unaccounted for: "
            + "; ".join(parts)
            + ". Pass carry_forward with the identifiers you intend to carry, or reconcile first."
        )
        err_cls: type[EditPreconditionError] = (
            ForkPreconditionError if edit_type == "fork" else EditPreconditionError
        )
        raise err_cls(
            message,
            edit_type=edit_type,
            derivation=derivation,
            unaccounted=unaccounted,
            rationale=rationale,
        )
    return derivation, carry_set, summary_payload(derivation)


enforce = check_preconditions


def render_preserved_summary(
    summary: dict[str, Any],
    carry_set: set[str] | None = None,
    *,
    anchor: int | None = None,
    edit_type: EditType | None = None,
) -> str:
    """One-liner carried-forward and preserved summary from a lineage summary.

    Built from the derivation summary that is stamped onto RUN_FORKED,
    RUN_RESTORED and RUN_MERGED events, so the text a human sees on success
    is exactly the audit trail the log carries. Counts are taken from the
    summary payload, not recomputed, and carry_forward is rendered verbatim
    so the audit shows what the operator asserted.
    """
    unsettled = summary.get("unsettled_authorizations", []) or []
    depended = summary.get("depended_results", []) or []
    uncertain = summary.get("uncertain_slots", []) or []
    carried = sorted(carry_set) if carry_set is not None else []
    carried_text = ", ".join(carried) if carried else "none"
    base = (
        f"preserved preconditions: {len(unsettled)} unsettled, "
        f"{len(depended)} depended, {len(uncertain)} uncertain; "
        f"carried forward: {carried_text}"
    )
    if anchor is not None and edit_type is not None:
        base = f"{edit_type} {base} at anchor {anchor}"
    elif anchor is not None:
        base = f"{base} at anchor {anchor}"
    return base


def render_refusal_text(
    rationale: dict[str, Any],
    *,
    run_id: str | None = None,
) -> str:
    """Human-readable refusal with named sequence numbers and reconcile hints.

    Every offending item is printed with its sequence number and primary
    identifier (approval_id, action_id or key) so a human can name the event
    to inspect. A short reconcile or carry-forward suggestion follows each
    block, because a refusal that names the problem but not the next step
    leaves the operator guessing. The text is produced once and colourised
    afterwards, so piped output stays byte-identical modulo colour codes.
    """
    edit_type = rationale.get("edit_type", "edit")
    anchor = rationale.get("anchor_sequence", rationale.get("divergence_sequence", "?"))
    candidate = rationale.get("candidate_sequence", "?")
    lines: list[str] = []
    header = f"[!!] {edit_type} refused: preconditions in (anchor {anchor}, head {candidate}] are unaccounted for"
    if run_id:
        header += f" for run {run_id}"
    lines.append(header)
    unsettled = rationale.get("unsettled_authorizations", []) or []
    if unsettled:
        lines.append(f"  [!!] {len(unsettled)} unsettled authorization(s):")
        for item in sorted(unsettled, key=lambda x: x.get("sequence", 0)):
            approval_id = item.get("approval_id", "?")
            seq = item.get("sequence", "?")
            subject = item.get("subject", "")
            detail = f" subject {subject!r}" if subject else ""
            lines.append(f"    - {approval_id} at sequence {seq}{detail}")
        lines.append(
            "    suggestion: revoke with APPROVAL_REVOKED or carry with --carry-forward <approval_id|sequence>"
        )
    depended = rationale.get("depended_results", []) or []
    if depended:
        lines.append(f"  [!!] {len(depended)} depended result(s) would be stranded:")
        for item in sorted(depended, key=lambda x: x.get("sequence", 0)):
            key = item.get("key", "?")
            action_id = item.get("action_id", "?")
            action_type = item.get("action_type", "?")
            seq = item.get("sequence", "?")
            lines.append(f"    - {action_id} (key {key}, type {action_type}) at sequence {seq}")
        lines.append(
            "    suggestion: carry with --carry-forward <key|action_id|sequence> if the result must survive the edit"
        )
    uncertain = rationale.get("uncertain_slots", []) or []
    if uncertain:
        lines.append(f"  [!!] {len(uncertain)} uncertain slot(s) still open:")
        for item in sorted(uncertain, key=lambda x: x.get("sequence", 0)):
            key = item.get("key", "?")
            action_id = item.get("action_id", "?")
            action_type = item.get("action_type", "?")
            status = item.get("status", "?")
            seq = item.get("sequence", "?")
            lines.append(
                f"    - {action_id} (key {key}, type {action_type}, status {status}) at sequence {seq}"
            )
        target = f" {run_id}" if run_id else ""
        lines.append(
            f"    suggestion: reconcile with `continuum reconcile{target}` or carry with --carry-forward <key|action_id>"
        )
    if not (unsettled or depended or uncertain):
        lines.append("  (no unaccounted items, rationale present for audit)")
    carry = rationale.get("carry_forward", []) or []
    if carry:
        lines.append(f"  carried forward so far: {', '.join(str(c) for c in carry)}")
    lines.append(
        "  hint: pass --carry-forward with the identifiers you intend to carry, or reconcile first."
    )
    return "\n".join(lines)


def stamp_lineage(
    storage: Storage,
    run_id: str,
    *,
    edit_type: EditType,
    anchor: int,
    summary: dict[str, Any],
    carry_set: set[str],
    extra: dict[str, Any] | None = None,
) -> None:
    """Stamp the derivation summary onto the audit log."""
    event_type = {
        "fork": EventType.RUN_FORKED,
        "restore": EventType.RUN_RESTORED,
        "merge": EventType.RUN_MERGED,
    }[edit_type]

    payload: dict[str, Any] = {
        "edit_type": edit_type,
        "anchor_sequence": anchor,
        "divergence_sequence": anchor,
        "candidate_sequence": storage.last_sequence(run_id),
        "preconditions": summary,
        "precondition_summary": summary,
        "derivation": summary,
        "derivation_summary": summary,
        "carry_forward": sorted(carry_set),
    }
    if extra:
        payload.update(extra)
    storage.append_event(run_id, event_type, payload, source=Origin.HUMAN)

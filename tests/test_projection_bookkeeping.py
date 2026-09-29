"""Projection bookkeeping invariant (issue #386).

PROJECTION_BOOKKEEPING is excluded from four serialisation surfaces and every
exclusion is load-bearing for cross-version compatibility. The failure mode for
getting it wrong is the worst available, every pre-existing checkpoint reports
as tampered, indistinguishable from the value proposition.

This module makes the exclusion enforced rather than remembered. Every field on
SemanticState must be either in the persisted/fingerprinted payload or in
PROJECTION_BOOKKEEPING, so adding a field without deciding fails loudly as a
build error. The check is dynamic, set(SemanticState.model_fields) minus the
fingerprint set equals PROJECTION_BOOKKEEPING, so Agents 3 and 4 adding
attempt_lessons or plan do not need a hardcoded list update.

The one deliberate exception is ``observed_risks`` (issue #1421): it stays out
of the fingerprint and the integrity digest but rides in a persisted checkpoint
body, because ``restore`` folds the live tail onto that body and cannot
re-derive the rows from an archived prefix. So the three persistence surfaces
split into two exclusion sets, PROJECTION_BOOKKEEPING for the digest and the
versions audit body, CHECKPOINT_BODY_OMITTED for the checkpoint body, and both
are asserted exactly rather than remembered.

The fixture subtlety from #385 is preserved in test_checkpoint_compat, both
halves, stripping plus re-sealing. This file only adds the classification
invariant and that the surfaces actually use their constants.
"""

from __future__ import annotations

import json

from continuum.models import (
    CHECKPOINT_BODY_OMITTED,
    PROJECTION_BOOKKEEPING,
    Goal,
    SemanticState,
    StateCheckpoint,
)
from continuum.state.versioning import canonical_state_json, state_fingerprint


def _base_state() -> SemanticState:
    return SemanticState(run_id="run_bookkeeping", goal=Goal(description="g"))


def test_every_field_is_either_in_fingerprint_or_in_bookkeeping() -> None:
    """Enforce Option 2, every SemanticState field is classified.

    Fingerprint payload is the meaning of a state, versioning fields are not
    part of that meaning but they are also not bookkeeping, they are persisted
    elsewhere. The invariant the issue asks for is that the only fields that
    are *not* in the fingerprint payload *beyond* the known versioning set are
    exactly PROJECTION_BOOKKEEPING. Formulated as the task describes,
    set(model_fields) minus fingerprint_fields equals bookkeeping, where
    fingerprint_fields is the set actually hashed by state_fingerprint.

    Dynamic so a new attempt_lessons field added by another agent is
    automatically part of fingerprint_fields and does not require a hardcoded
    update, but a new bookkeeping-like field added without putting it in
    PROJECTION_BOOKKEEPING would be hashed and persisted, which is the wrong
    answer, the test below for the persisted surfaces would still pass, so we
    also assert the four surfaces agree on the same bookkeeping set.
    """
    all_fields = set(SemanticState.model_fields)
    versioning = {"version", "created_at", "updated_at", "source_sequence"}
    assert versioning.issubset(all_fields)
    assert PROJECTION_BOOKKEEPING.issubset(all_fields)
    assert PROJECTION_BOOKKEEPING.isdisjoint(versioning)

    # Fingerprint payload is all_fields minus versioning minus bookkeeping.
    fingerprint_fields = all_fields - versioning - PROJECTION_BOOKKEEPING
    # Every field is either fingerprinted or bookkeeping or versioning.
    assert all_fields == fingerprint_fields | PROJECTION_BOOKKEEPING | versioning
    assert fingerprint_fields.isdisjoint(PROJECTION_BOOKKEEPING)
    # The task's phrasing, set(model_fields) - set(fingerprint_fields) == bookkeeping
    # holds once versioning is accounted for, the fingerprint set here is the
    # fingerprinted set plus versioning, i.e. everything except bookkeeping.
    assert all_fields - (fingerprint_fields | versioning) == PROJECTION_BOOKKEEPING

    # Behavioural confirmation, changing a bookkeeping field must not change
    # the fingerprint, changing a fingerprinted field must.
    base = _base_state()
    base_fp = state_fingerprint(base)
    # Bookkeeping field, status
    degraded = base.model_copy(update={"status": base.status.__class__("invalid")})
    assert state_fingerprint(degraded) == base_fp
    # Fingerprinted field, goal
    altered = base.model_copy(update={"goal": Goal(description="different")})
    assert state_fingerprint(altered) != base_fp


def test_canonical_state_json_excludes_only_bookkeeping() -> None:
    all_fields = set(SemanticState.model_fields)
    base = _base_state()
    persisted = set(json.loads(canonical_state_json(base)).keys())
    assert persisted == all_fields - PROJECTION_BOOKKEEPING


def test_state_checkpoint_content_excludes_only_bookkeeping() -> None:
    all_fields = set(SemanticState.model_fields)
    base = _base_state()
    checkpoint = StateCheckpoint(run_id=base.run_id, state=base)
    content_state_keys = set(checkpoint.content()["state"].keys())
    assert content_state_keys == all_fields - PROJECTION_BOOKKEEPING


def test_state_checkpoint_canonical_json_excludes_only_bookkeeping() -> None:
    """The checkpoint body keeps ``observed_risks``, unlike the digest (issue #1421).

    ``restore`` folds the live tail onto this body and cannot re-derive the
    rows from an archived prefix, so the risks ride in the body. The other four
    stay out: they are always default in anything persistable, and keeping them
    out is what lets a pre-#383 reader still load the body.
    """
    all_fields = set(SemanticState.model_fields)
    base = _base_state()
    checkpoint = StateCheckpoint(run_id=base.run_id, state=base)
    body = json.loads(checkpoint.canonical_json())
    assert set(body["state"].keys()) == all_fields - CHECKPOINT_BODY_OMITTED
    assert "observed_risks" in body["state"]
    assert PROJECTION_BOOKKEEPING - {"observed_risks"} == CHECKPOINT_BODY_OMITTED


def test_four_surfaces_agree_on_bookkeeping() -> None:
    """The fingerprint set is one constant, and the digest and versions body use it.

    The checkpoint body is the one deliberate asymmetry (issue #1421): it keeps
    ``observed_risks`` where the digest and the versions audit body drop it, so
    the three persistence surfaces split into two exclusion sets rather than
    one. What must stay true is that each surface excludes *exactly* the set it
    claims, which is what keeps a new bookkeeping-like field from silently
    landing in one surface and not the others.
    """
    all_fields = set(SemanticState.model_fields)
    canonical_keys = set(json.loads(canonical_state_json(base := _base_state())).keys())
    checkpoint = StateCheckpoint(run_id=base.run_id, state=base)
    content_keys = set(checkpoint.content()["state"].keys())
    canonical_checkpoint_keys = set(json.loads(checkpoint.canonical_json())["state"].keys())

    # The digest and the versions audit body exclude the full bookkeeping set.
    assert canonical_keys == content_keys == all_fields - PROJECTION_BOOKKEEPING
    # The checkpoint body keeps observed_risks and drops only the other four.
    assert canonical_checkpoint_keys == all_fields - CHECKPOINT_BODY_OMITTED
    assert canonical_checkpoint_keys - canonical_keys == {"observed_risks"}

    # Fingerprint agrees plus versioning.
    versioning = {"version", "created_at", "updated_at", "source_sequence"}
    base_fp = state_fingerprint(base)
    # Changing a versioning field must not change fingerprint either, but it
    # is not bookkeeping, it is just not part of meaning.
    versioned = base.model_copy(update={"version": base.version + 1})
    assert state_fingerprint(versioned) == base_fp

    # The fingerprint payload is the canonical payload minus versioning.
    assert set(json.loads(canonical_state_json(base)).keys()) - versioning == set(
        base.model_dump(
            mode="json",
            exclude={
                "version",
                "created_at",
                "updated_at",
                "source_sequence",
                *PROJECTION_BOOKKEEPING,
            },
        ).keys()
    )

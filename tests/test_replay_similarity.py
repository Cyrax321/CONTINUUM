"""Semantic similarity backends for the replay guard (issue #291)."""

from __future__ import annotations

import pytest

from continuum.replay_similarity import (
    SimilarityConfig,
    SimilarityKind,
    _cosine,
    classify_call,
    jaccard,
    similarity,
    similarity_backend,
    token_set,
)


def test_jaccard_identical_is_one() -> None:
    assert jaccard(token_set("acme INV-001"), token_set("INV-001 acme")) == 1.0


def test_jaccard_disjoint_is_zero() -> None:
    assert jaccard(token_set("foo"), token_set("bar")) == 0.0


def test_fuzzy_catches_paraphrased_arguments() -> None:
    cfg = SimilarityConfig(kind="fuzzy", replay_threshold=0.70)
    prior = {"customer": "acme corp", "invoice_id": "INV-001", "amount": "100"}
    score = similarity(
        {"invoice_id": "INV-001", "amount": "100", "customer": "acme"},
        prior,
        cfg,
    )
    assert score >= cfg.replay_threshold


def test_fuzzy_rejects_different_arguments() -> None:
    cfg = SimilarityConfig(kind="fuzzy", replay_threshold=0.85)
    score = similarity(
        {"target": "/etc/passwd", "mode": "overwrite"},
        {"customer": "acme", "amount": 100},
        cfg,
    )
    assert score < 0.5


def test_exact_kind_still_works() -> None:
    cfg = SimilarityConfig(kind="exact")
    assert similarity({"a": 1}, {"a": 1}, cfg) == 1.0
    assert similarity({"a": 1}, {"b": 2}, cfg) == 0.0


# --- argument flattening (issue #1865) --------------------------------------------- #


def test_shared_values_under_different_keys_are_not_identical() -> None:
    """``_args_text`` must carry keys, or two unrelated calls collapse to one.

    Before, both of these flattened to ``"ACC-999 100"``: EXACT scored 1.0 and
    the guard returned the cached result of a completely different operation.
    """
    exact = SimilarityConfig(kind="exact")
    fuzzy = SimilarityConfig(kind="fuzzy")
    to_account = {"recipient_account": "ACC-999", "amount": 100}
    from_account = {"sender_account": "ACC-999", "fee": 100}

    assert similarity(to_account, from_account, exact) == 0.0
    assert similarity(to_account, from_account, fuzzy) < fuzzy.fork_threshold


def test_equal_dictionaries_match_regardless_of_insertion_order() -> None:
    """Canonical text is sorted, so equal dicts score 1.0 under EXACT.

    Before, insertion order alone made the same call look completely fresh,
    silently defeating replay deduplication.
    """
    cfg = SimilarityConfig(kind="exact")
    call_a = {"user": "alice", "repo": "continuum"}
    call_b = {"repo": "continuum", "user": "alice"}

    assert call_a == call_b
    assert similarity(call_a, call_b, cfg) == 1.0
    assert similarity(call_a, {"user": "alice", "repo": "continuum2"}, cfg) == 0.0


def test_nested_arguments_flatten_canonically() -> None:
    """A nested mapping is sorted too, not stringified in caller order."""
    cfg = SimilarityConfig(kind="exact")

    assert similarity({"meta": {"b": 1, "a": 2}}, {"meta": {"a": 2, "b": 1}}, cfg) == 1.0

    # and unserialisable values still flatten deterministically
    class Opaque:
        def __str__(self) -> str:
            return "opaque-value"

    assert similarity({"obj": Opaque(), "n": 1}, {"n": 1, "obj": Opaque()}, cfg) == 1.0


# --- classify_call ---------------------------------------------------------------- #


PRIOR = {
    "k1": {
        "action_type": "send_invoice",
        "status": "completed",
        "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        "__ledger_key__": "k1",
    },
}


def _cfg(threshold: float = 0.85) -> SimilarityConfig:
    return SimilarityConfig(kind="fuzzy", replay_threshold=threshold)


def test_same_intent_after_restore_returns_replay() -> None:
    kind, match = classify_call(
        new_key="any-rendering",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=PRIOR,
        config=_cfg(),
        run_id="run_1",
    )
    assert kind == "replay"
    assert match is not None
    assert match["action_type"] == "send_invoice"


def test_different_intent_returns_fresh() -> None:
    kind, match = classify_call(
        new_key="other-key",
        new_args={"target": "/etc/passwd"},
        action_type="send_invoice",
        prior_actions=PRIOR,
        config=_cfg(),
        run_id="run_1",
    )
    assert kind == "fresh"
    assert match is None


def test_cross_type_matching_is_never_performed() -> None:
    prior = {
        "k2": {
            "action_type": "charge_card",
            "status": "completed",
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        }
    }
    kind, match = classify_call(
        new_key="anything",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=prior,
        config=_cfg(0.5),
        run_id="run_1",
    )
    assert kind == "fresh"
    del match


@pytest.mark.parametrize(
    "status",
    ["failed", "started", "unknown", "compensated", "planned", "requires_review", "expired"],
)
def test_non_completed_prior_action_is_never_replayed(status: str) -> None:
    # issue #1837: the similarity scan used to match prior actions regardless
    # of status, so a failed action looked like a replay hit and blocked
    # re-execution of the failed operation.
    prior = {
        "k_non_completed": {
            "action_type": "send_invoice",
            "status": status,
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        }
    }
    kind, match = classify_call(
        new_key="paraphrased_key",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=prior,
        config=_cfg(),
        run_id="run_1",
    )
    assert kind == "fresh"
    assert match is None


@pytest.mark.parametrize("status", ["failed", "started", "unknown"])
def test_non_completed_prior_action_is_never_a_fork(status: str) -> None:
    # The scan feeds both replay and fork decisions, so a non-completed
    # action must not steer a partially-similar call into "fork" either.
    prior = {
        "k_non_completed": {
            "action_type": "send_invoice",
            "status": status,
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        }
    }
    cfg = SimilarityConfig(kind="fuzzy", replay_threshold=0.85, fork_threshold=0.40)
    kind, match = classify_call(
        new_key="paraphrased_key",
        new_args={"invoice_id": "INV-001", "customer": "globex"},
        action_type="send_invoice",
        prior_actions=prior,
        config=cfg,
        run_id="run_1",
    )
    assert kind == "fresh"
    assert match is None


def test_failed_prior_action_does_not_mask_completed_replay_hit() -> None:
    # A failed attempt and a completed attempt both exist. The completed one
    # must remain the match, so the cached result is still returned.
    prior = {
        "k_failed": {
            "action_type": "send_invoice",
            "status": "failed",
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        },
        "k_completed": {
            "action_type": "send_invoice",
            "status": "completed",
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        },
    }
    kind, match = classify_call(
        new_key="paraphrased_key",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=prior,
        config=_cfg(),
        run_id="run_1",
    )
    assert kind == "replay"
    assert match is not None
    assert match["status"] == "completed"


def test_cosine_unequal_length_embeddings_norm() -> None:
    # issue #1608: _cosine computes norm over the full vectors, not truncated zip pairs
    a = [1.0, 0.0]
    b = [3.0, 4.0, 100.0]
    score = _cosine(a, b)
    # The true cosine is ~0.02996. The truncated zip used to report 0.6.
    assert score < 0.05
    assert _cosine(b, a) == score


def test_cosine_clamps_negative_to_zero() -> None:
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == 0.0


def test_classify_call_embedding_ragged_vector_no_false_fork() -> None:
    # Ragged embedding vectors must not misclassify as fork (issue #1608)
    cfg = SimilarityConfig(
        kind="embedding",
        embedder=lambda t: [1.0, 0.0] if "target" in t else [3.0, 4.0, 100.0],
    )
    kind, match = classify_call(
        new_key="k_new",
        new_args={"k": "target"},
        action_type="send_invoice",
        prior_actions={
            "k_old": {
                "action_type": "send_invoice",
                "arguments": {"k": "different"},
                "status": "completed",
            }
        },
        config=cfg,
        run_id="run_1",
    )
    assert kind == "fresh"
    assert match is None


# --- similarity_backend (issue #1654) --------------------------------------------- #


def test_similarity_backend_passes_through_existing_config() -> None:
    cfg = SimilarityConfig(kind="fuzzy", replay_threshold=0.7)
    assert similarity_backend(cfg) is cfg


@pytest.mark.parametrize(
    ("name", "expected_kind"),
    [
        ("exact", SimilarityKind.EXACT),
        ("fuzzy", SimilarityKind.FUZZY),
        ("embedding", SimilarityKind.EMBEDDING),
    ],
)
def test_similarity_backend_maps_registry_name_to_kind(
    name: str, expected_kind: SimilarityKind
) -> None:
    cfg = similarity_backend(name)
    assert cfg.kind == expected_kind


def test_similarity_backend_uses_config_defaults_for_thresholds() -> None:
    cfg = similarity_backend("fuzzy")
    assert cfg.replay_threshold == SimilarityConfig().replay_threshold
    assert cfg.fork_threshold == SimilarityConfig().fork_threshold


def test_similarity_backend_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown similarity backend 'nope'"):
        similarity_backend("nope")


def test_similarity_backend_unknown_name_quotes_input() -> None:
    # The error message must show the offending value so operators can fix it.
    with pytest.raises(ValueError, match="'not-a-backend'"):
        similarity_backend("not-a-backend")


def test_classify_call_no_longer_replays_a_different_operation() -> None:
    """The end-to-end shape from the report: same values under different keys
    used to classify as ``replay`` and hand back the cached result of an
    unrelated completed operation."""
    cfg = SimilarityConfig(kind="exact")
    prior = {
        "k1": {
            "action_type": "transfer_funds",
            "arguments": {"sender_account": "ACC-999", "fee": 100},
            "status": "completed",
            "result": {"transferred": True},
        }
    }

    kind, matched = classify_call(
        new_key="k_new",
        new_args={"recipient_account": "ACC-999", "amount": 100},
        action_type="transfer_funds",
        prior_actions=prior,
        config=cfg,
        run_id="run_1",
    )

    assert kind == "fresh"
    assert matched is None

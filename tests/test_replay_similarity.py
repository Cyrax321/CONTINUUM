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


@pytest.mark.parametrize("status", ["failed", "started", "unknown", None])
def test_non_completed_prior_action_is_not_replayed(status: str | None) -> None:
    # A prior action that failed, timed out, or remains started must not turn a
    # similar post-restore call into a replay hit -- the work has to run again.
    prior = {
        "k_failed": {
            "action_type": "send_invoice",
            "status": status,
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        }
    }
    kind, match = classify_call(
        new_key="paraphrased-key",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=prior,
        config=_cfg(),
        run_id="run_1",
    )
    assert kind == "fresh"
    assert match is None


def test_non_completed_prior_action_is_not_matched_as_fork() -> None:
    # Same gate for the fork band: a non-completed action must not be reported
    # as a near-miss match either, otherwise callers would treat it as related.
    prior = {
        "k_failed": {
            "action_type": "send_invoice",
            "status": "failed",
            "arguments": {"customer": "acme", "invoice_id": "INV-001"},
        }
    }
    kind, match = classify_call(
        new_key="paraphrased-key",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=prior,
        config=_cfg(),
        run_id="run_1",
    )
    assert kind == "fresh"
    assert match is None


def test_completed_action_still_matches_below_and_above_fork_threshold() -> None:
    # The status gate must not weaken matching for genuinely completed actions.
    kind, match = classify_call(
        new_key="paraphrased-key",
        new_args={"invoice_id": "INV-001", "customer": "acme"},
        action_type="send_invoice",
        prior_actions=PRIOR,
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
        embedder=lambda t: [1.0, 0.0] if t == "target" else [3.0, 4.0, 100.0],
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

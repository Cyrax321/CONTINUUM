"""Semantic similarity backends for the replay guard (issue #291)."""

from __future__ import annotations

from continuum.replay_similarity import (
    SimilarityConfig,
    classify_call,
    jaccard,
    similarity,
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


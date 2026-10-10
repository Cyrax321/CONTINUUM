"""Semantic similarity backends for the replay guard (issue #291)."""

from __future__ import annotations

import pytest

from continuum.replay_similarity import (
    SimilarityConfig,
    SimilarityKind,
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


# --- similarity_backend: registry entry -> config (issue #1029) ------------------ #


def embed_text(text: str) -> list[float]:
    """Stand-in for an operator-supplied embedder (returns a real vector)."""
    return [1.0, 0.0]


def test_backend_name_builds_default_thresholds() -> None:
    cfg = similarity_backend("fuzzy")
    assert cfg.kind is SimilarityKind.FUZZY
    # The documented defaults stay put when only the name is given.
    assert cfg.replay_threshold == SimilarityConfig.replay_threshold
    assert cfg.fork_threshold == SimilarityConfig.fork_threshold


def test_backend_mapping_tunes_the_thresholds() -> None:
    cfg = similarity_backend({"kind": "fuzzy", "replay_threshold": 0.7, "fork_threshold": 0.3})
    assert cfg.kind is SimilarityKind.FUZZY
    assert cfg.replay_threshold == 0.7
    assert cfg.fork_threshold == 0.3


def test_backend_passes_a_built_config_through_untouched() -> None:
    built = SimilarityConfig(kind="fuzzy", replay_threshold=0.6)
    assert similarity_backend(built) is built


def test_backend_rejects_an_unknown_kind_with_the_valid_choices() -> None:
    with pytest.raises(ValueError, match="unknown similarity backend 'cosine'"):
        similarity_backend({"kind": "cosine"})


def test_backend_rejects_thresholds_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match=r"replay_threshold must be within \[0.0, 1.0\]"):
        similarity_backend({"kind": "fuzzy", "replay_threshold": 1.5})


def test_backend_rejects_a_fork_band_above_the_replay_band() -> None:
    with pytest.raises(ValueError, match="must not exceed replay_threshold"):
        similarity_backend({"kind": "fuzzy", "replay_threshold": 0.4, "fork_threshold": 0.8})


def test_embedding_requires_a_resolvable_embedder_path() -> None:
    with pytest.raises(ValueError, match="needs an 'embedder' dotted path"):
        similarity_backend({"kind": "embedding"})


def test_backend_resolves_an_embedder_from_a_dotted_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registry entry cannot hold a function, so an operator names one by
    # dotted path. Resolving it at build time means a typo fails when the gate
    # config is loaded, not on the first gated call. The throwaway module
    # stands in for an operator's embedder package.
    import sys
    import types

    mod = types.ModuleType("continuum_test_embedder")
    mod.embed_text = embed_text
    monkeypatch.setitem(sys.modules, "continuum_test_embedder", mod)

    cfg = similarity_backend(
        {"kind": "embedding", "embedder": "continuum_test_embedder.embed_text"}
    )
    assert cfg.kind is SimilarityKind.EMBEDDING
    assert cfg.embedder is embed_text
    assert cfg.embedder("pay invoice INV-001") == [1.0, 0.0]


def test_backend_rejects_an_embedder_path_that_does_not_resolve() -> None:
    with pytest.raises(ValueError, match="cannot resolve embedder"):
        similarity_backend({"kind": "embedding", "embedder": "no_such_module.embed"})

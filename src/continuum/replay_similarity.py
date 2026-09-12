"""Semantic similarity backends for the replay guard (issue #291).

Exact key matching fails when an LLM renders the same intent with different
argument text ("pay invoice INV-001" vs "settle outstanding amount for
INV-001"). These backends classify post-restore calls as replay, fork, or
divergent using configurable comparison strategies.

Three backends ship:

- ``exact``: current behaviour (sha256 of normalised arguments)
- ``fuzzy``: token-set Jaccard over stringified argument values (stdlib only,
  sub-millisecond, catches LLM paraphrasing without any external service)
- ``embedding``: caller-supplied callable that maps text to a float vector;
  CONTINUUM bundles no embedding model.

All backends are deterministic and synchronous: the gate must classify in
sub-millisecond time without network calls or model inference.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "SimilarityKind",
    "SimilarityConfig",
    "token_set",
    "jaccard",
    "similarity_backend",
    "classify_call",
]


class SimilarityKind(StrEnum):
    """Comparison strategies available to the replay guard."""

    EXACT = "exact"
    FUZZY = "fuzzy"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class SimilarityConfig:
    """Configure the similarity strategy and replay/fork score thresholds."""

    kind: SimilarityKind = SimilarityKind.EXACT
    """Which comparison strategy to use."""
    replay_threshold: float = 0.90
    """Above this similarity: same intent, return cached result."""
    fork_threshold: float = 0.50
    """Between fork and replay thresholds: divergent, require fork."""
    embedder: Callable[[str], list[float]] | None = None
    """Caller-supplied embedding function (required when kind is EMBEDDING)."""


def token_set(text: str) -> frozenset[str]:
    """Lowercase word tokens above length 1, punctuation stripped."""
    return frozenset(w.lower() for w in re.findall(r"[a-z0-9_]{2,}", text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Return token-set similarity, treating two empty sets as identical."""

    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _args_text(args: dict[str, Any]) -> str:
    """Flatten arguments into comparable text."""
    parts = []
    for v in args.values():
        parts.append(str(v))
    return " ".join(parts)


def _cosine(a: list[float], b: list[float]) -> float:
    pairs = [(x, y) for x, y in zip(a, b, strict=False)]
    dot = float(sum(x * y for x, y in pairs))
    norm_a = float(sum(x * x for x, _ in pairs)) ** 0.5
    norm_b = float(sum(y * y for _, y in pairs)) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def similarity(
    candidate_args: dict[str, Any],
    prior_args: dict[str, Any],
    config: SimilarityConfig,
) -> float:
    """Score how similar two argument dicts are, in [0, 1]."""
    ctext = _args_text(candidate_args)
    ptext = _args_text(prior_args)

    if config.kind == SimilarityKind.EXACT:
        return 1.0 if ctext == ptext else 0.0
    if config.kind == SimilarityKind.FUZZY:
        return jaccard(token_set(ctext), token_set(ptext))
    if config.kind == SimilarityKind.EMBEDDING:
        if config.embedder is None:
            raise ValueError("EMBEDDING kind requires an embedder function")
        return float(max(0.0, min(1.0, _cosine(config.embedder(ctext), config.embedder(ptext)))))
    return 0.0


def classify_call(
    new_key: str,
    new_args: dict[str, Any],
    action_type: str,
    prior_actions: dict[str, dict[str, Any]],
    config: SimilarityConfig,
    run_id: str,
) -> tuple[str, dict[str, Any] | None]:
    """Classify a post-restore call against prior completed actions.

    Returns ``(classification, matched_action)`` where classification is one
    of ``"replay"``, ``"fork"``, or ``"fresh"``.

    Only actions of the SAME type are compared; cross-type matching is never
    performed regardless of similarity score.
    """
    from continuum.actions.idempotency import idempotency_key

    exact_key = str(idempotency_key(action_type, None, scope=run_id, key=new_key))
    if exact_key in prior_actions:
        prior = prior_actions[exact_key]
        if prior.get("status") == "completed":
            return "replay", prior
        return "fresh", None

    best_score = 0.0
    best_action: dict[str, Any] | None = None
    for key, action in prior_actions.items():
        if action.get("action_type") != action_type:
            continue
        prior_args_raw = action.get("arguments") or {}
        if not isinstance(prior_args_raw, dict):
            continue
        score = similarity(new_args, prior_args_raw, config)
        if score > best_score:
            best_score = score
            best_action = {**action, "__ledger_key__": key}

    if best_score >= config.replay_threshold:
        return "replay", best_action
    if best_score >= config.fork_threshold:
        return "fork", best_action
    return "fresh", None


def similarity_backend(name_or_config: str | SimilarityConfig) -> SimilarityConfig:
    """Build a SimilarityConfig from a registry entry name or explicit config."""
    if isinstance(name_or_config, SimilarityConfig):
        return name_or_config
    kind_map: dict[str, SimilarityKind] = {
        "exact": SimilarityKind.EXACT,
        "fuzzy": SimilarityKind.FUZZY,
        "embedding": SimilarityKind.EMBEDDING,
    }
    kind = kind_map.get(name_or_config)
    if kind is None:
        raise ValueError(f"unknown similarity backend {name_or_config!r}")
    return SimilarityConfig(kind=kind)


# Keep unused imports referenced for mypy strict
_ = json

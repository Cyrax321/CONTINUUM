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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

__all__ = [
    "SimilarityKind",
    "SimilarityConfig",
    "token_set",
    "jaccard",
    "similarity_backend",
    "classify_call",
]


class SimilarityKind(StrEnum):
    """Strategies used to compute replay similarity scores.

    The selected strategy supplies the score used for replay and fork
    decisions.
    """

    EXACT = "exact"
    FUZZY = "fuzzy"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class SimilarityConfig:
    """Configure similarity scoring and replay/fork decisions.

    ``kind`` selects the comparison strategy. The replay and fork thresholds
    classify the score produced by that strategy.
    """

    kind: SimilarityKind = SimilarityKind.EXACT
    """Which comparison strategy to use."""
    replay_threshold: float = 0.90
    """Above this similarity: same intent, return cached result."""
    fork_threshold: float = 0.50
    """Between fork and replay thresholds: divergent, require fork."""
    embedder: Callable[[str], list[float]] | None = None
    """Caller-supplied embedding function (required when kind is EMBEDDING)."""

    def __post_init__(self) -> None:
        # The dataclass does not coerce the way a pydantic model would, so a
        # caller passing the bare string ("fuzzy") would leave kind as a str.
        # Downstream comparisons and rendering expect the enum, including the
        # gate's fail-closed exact-vs-not check in replayguard.evaluate.
        if not isinstance(self.kind, SimilarityKind):
            object.__setattr__(self, "kind", SimilarityKind(self.kind))


def token_set(text: str) -> frozenset[str]:
    """Lowercase word tokens above length 1, punctuation stripped."""
    return frozenset(w.lower() for w in re.findall(r"[a-z0-9_]{2,}", text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Return token-set similarity.

    Two empty sets score ``1.0``. If only one set is empty, the score is
    ``0.0``.
    """

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


def _resolve_embedder(spec: Any) -> Callable[[str], list[float]] | None:
    """Resolve an ``embedder`` entry to a callable, or None when absent.

    A JSON registry cannot hold a function, so the entry is a dotted path
    (``"my.package.embed_text"``) resolved by import, the same convention the
    reconciler registry uses for operator-supplied plugins. Resolved here rather
    than at classification time so a bad path fails when the config is loaded,
    not on the first gated call.
    """
    if spec is None:
        return None
    if callable(spec):
        return cast("Callable[[str], list[float]]", spec)
    if not isinstance(spec, str):
        raise ValueError(f"embedder must be a dotted path or callable, got {type(spec).__name__}")
    module_name, _, attr = spec.partition(":")
    if ":" not in spec:
        module_name, _, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"embedder must be a dotted path like 'pkg.mod.func', got {spec!r}")
    import importlib

    try:
        module = importlib.import_module(module_name)
        resolved: Any = getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"cannot resolve embedder {spec!r}: {exc}") from exc
    if not callable(resolved):
        raise ValueError(f"embedder {spec!r} is not callable")
    return cast("Callable[[str], list[float]]", resolved)


def similarity_backend(
    name_or_config: str | Mapping[str, Any] | SimilarityConfig,
) -> SimilarityConfig:
    """Build a :class:`SimilarityConfig` from a registry entry.

    Accepts the bare backend name (``"fuzzy"``), an explicit mapping
    (``{"kind": "fuzzy", "replay_threshold": 0.85}``) so an operator can tune the
    thresholds from the gate registry, or an already-built config.
    """
    if isinstance(name_or_config, SimilarityConfig):
        return name_or_config
    if isinstance(name_or_config, str):
        kind_map: dict[str, SimilarityKind] = {
            "exact": SimilarityKind.EXACT,
            "fuzzy": SimilarityKind.FUZZY,
            "embedding": SimilarityKind.EMBEDDING,
        }
        kind = kind_map.get(name_or_config)
        if kind is None:
            raise ValueError(f"unknown similarity backend {name_or_config!r}")
        return SimilarityConfig(kind=kind)
    if not isinstance(name_or_config, Mapping):
        raise ValueError(
            f"similarity config must be a name, mapping or SimilarityConfig, "
            f"got {type(name_or_config).__name__}"
        )
    raw_kind = name_or_config.get("kind", "exact")
    try:
        kind = SimilarityKind(raw_kind)
    except ValueError as exc:
        raise ValueError(
            f"unknown similarity backend {raw_kind!r} (expected one of "
            f"{', '.join(k.value for k in SimilarityKind)})"
        ) from exc
    replay_threshold = name_or_config.get("replay_threshold")
    fork_threshold = name_or_config.get("fork_threshold")
    embedder = _resolve_embedder(name_or_config.get("embedder"))
    if kind is SimilarityKind.EMBEDDING and embedder is None:
        raise ValueError(
            "embedding similarity needs an 'embedder' dotted path "
            '(for example "my.package.embed_text")'
        )
    kwargs: dict[str, Any] = {"kind": kind}
    if replay_threshold is not None:
        kwargs["replay_threshold"] = float(replay_threshold)
    if fork_threshold is not None:
        kwargs["fork_threshold"] = float(fork_threshold)
    if embedder is not None:
        kwargs["embedder"] = embedder
    # The thresholds decide whether a call is suppressed as a duplicate or let
    # through as new work, so an out-of-range value silently changes safety
    # behaviour. Reject it at load time instead of waiting for the first call.
    final_replay = kwargs.get("replay_threshold", SimilarityConfig.replay_threshold)
    final_fork = kwargs.get("fork_threshold", SimilarityConfig.fork_threshold)
    for name, value in (("replay_threshold", final_replay), ("fork_threshold", final_fork)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be within [0.0, 1.0], got {value}")
    if final_fork > final_replay:
        raise ValueError(
            f"fork_threshold ({final_fork}) must not exceed replay_threshold "
            f"({final_replay}): the fork band has to sit below the replay band"
        )
    return SimilarityConfig(**kwargs)


# Keep unused imports referenced for mypy strict
_ = json

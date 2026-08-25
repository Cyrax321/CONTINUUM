"""Deciding whether two action attempts are *the same* action.

An idempotency key answers one question: has this exact operation already been
performed? Get it wrong in one direction and the agent duplicates a side effect;
wrong in the other and it refuses to do legitimate new work.

The key is derived from the action type plus its arguments, canonically hashed —
so argument order never matters, but a changed value always does.

Volatile arguments
------------------

Some arguments differ on every call without changing what the operation *means*:
a retry counter, a client-generated request id, a timestamp. Left in the key
they would defeat deduplication entirely, since every retry would look like a
new action. ``volatile`` names the fields to exclude.

This is a sharp edge, so it is opt-in and explicit. Excluding a field that
genuinely distinguishes two operations would collapse them into one and silently
skip real work — the failure mode is quiet, which makes it worse than the noisy
one. Nothing is excluded by default.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from continuum.security.hashing import stable_hash

__all__ = [
    "idempotency_key",
    "arguments_hash",
    "identity_tokens",
    "leaf_tokens",
    "location_tokens",
    "locations_agree",
    "same_location",
    "IdempotencyKey",
]


class IdempotencyKey(str):
    """A content-derived identity for an action attempt.

    A plain string subclass so it serializes and compares transparently, but
    distinct in type signatures where the distinction matters.
    """

    __slots__ = ()


def _strip_volatile(arguments: Mapping[str, Any], volatile: Iterable[str]) -> dict[str, Any]:
    excluded = set(volatile)
    return {k: v for k, v in arguments.items() if k not in excluded}


def _canonicalize_paths(value: Any) -> Any:
    """Normalize path-like strings so equivalent spellings hash identically.

    Only values that look like local filesystem paths are touched (they contain
    a separator and are not URLs), and normalization is purely lexical
    (``normpath`` plus ``~`` expansion). It never resolves against the process
    working directory, so the result is deterministic on any machine.
    """
    if isinstance(value, str):
        if "://" not in value and ("/" in value or "\\" in value):
            return os.path.normpath(os.path.expanduser(value))
        return value
    if isinstance(value, Mapping):
        return {k: _canonicalize_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonicalize_paths(v) for v in value]
    if isinstance(value, tuple):
        return [_canonicalize_paths(v) for v in value]
    return value


def _strip_and_canonicalize(arguments: Mapping[str, Any] | None, volatile: Iterable[str]) -> Any:
    return _canonicalize_paths(_strip_volatile(arguments or {}, volatile))


def arguments_hash(
    arguments: Mapping[str, Any] | None = None,
    *,
    volatile: Iterable[str] = (),
) -> str:
    """Canonical hash of an action's arguments.

    Key order is irrelevant; values are not. Path-like string values are
    normalized first (``normpath`` + ``~`` expansion), so equivalent spellings
    of the same path hash identically. Raises if an argument cannot be hashed
    deterministically, because a key that changes between runs would silently
    disable deduplication.
    """
    return stable_hash(_strip_and_canonicalize(arguments, volatile))


def idempotency_key(
    action_type: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    scope: str | None = None,
    volatile: Iterable[str] = (),
    key: str | None = None,
) -> IdempotencyKey:
    """Derive a stable key identifying this operation.

    ``scope`` narrows the key, typically to a run, so two runs performing the
    same logical operation do not deduplicate against each other within that
    ledger. A narrower scope (for example, a run id) is what most callers want.

    ``scope`` narrows the key, typically to a run, so two runs performing the
    same logical operation do not deduplicate against each other within that
    ledger. A narrower scope (for example, a run id) is what most callers want.

    Note: ``scope`` only shapes the derived key. With the default per-run scope
    the key is unique to that run. With ``scope=None`` (``scoped_to_run=False``)
    the key is store-global and ``ActionLedger.claim`` consults every run's log,
    so a completed record anywhere deduplicates the claim and an unresolved
    attempt elsewhere raises instead of opening a parallel slot (issue 34).

    ``key`` overrides argument hashing entirely, in the style of Stripe's
    ``Idempotency-Key``. Argument hashing assumes identical arguments mean the
    same operation, which is wrong for actions that are legitimately repeated:
    sending the same reminder twice is two sends, not one, and hashing would
    silently drop the second. When the caller knows the operation's identity,
    it should say so rather than encode it by perturbing the arguments.
    """
    if not action_type.strip():
        # Checked on both branches, not only the derived-key one. The action type
        # is the unit the retry budget counts and the key the reconciler registry
        # matches on, so a blank one is silently exempt from both: it matches no
        # probe and pools every unnamed action into a single budget. Supplying an
        # explicit ``key`` used to skip this check entirely.
        raise ValueError("action_type must be a non-empty string")

    if key is not None:
        if not key:
            raise ValueError("explicit idempotency key must be a non-empty string")
        return IdempotencyKey(stable_hash({"scope": scope, "type": action_type, "key": key}))

    return IdempotencyKey(
        stable_hash(
            {
                "scope": scope,
                "type": action_type,
                "arguments": _strip_and_canonicalize(arguments, volatile),
            }
        )
    )


_WEAK_TOKENS = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "tmp",
        "var",
        "home",
        "user",
        "bin",
        "etc",
        "usr",
        "log",
        "logs",
        "out",
        "in",
        "on",
        "sent",
        "file",
    }
)

# Generic words that are far more likely to be incidental argument names or
# filler than a resource identity. A string like this is not distinctive enough
# to drive the defensive drift fallback, so it is dropped as a token.
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "then",
        "when",
        "what",
        "which",
        "who",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "above",
        "below",
        "to",
        "of",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "send",
        "update",
        "create",
        "delete",
        "read",
        "write",
        "get",
        "set",
        "add",
        "remove",
        "make",
        "take",
        "give",
        "show",
        "list",
        "find",
        "run",
        "start",
        "stop",
        "open",
        "close",
        "name",
        "type",
        "status",
        "value",
        "data",
        "info",
        "item",
        "key",
        "field",
        "result",
        "note",
        "text",
        "kind",
        "mode",
        "state",
        "event",
        "action",
        "request",
        "response",
        "error",
        "message",
        "account",
        "description",
    }
)


def _is_strong_token(token: str) -> bool:
    """A token distinctive enough to identify a resource.

    Any value that is not filler counts. A plain word (``invoice``, ``dataset``)
    names a resource just as well as ``INV-001`` does (issue #33), and a purely
    numeric string is a legitimate identity too -- a row id, an account number,
    an invoice number rendered as digits (issue #36). What gets dropped is only
    what cannot distinguish one resource from another: tokens too short to be
    meaningful, the explicit weak list, and generic stopwords.

    Admitting plain words is only safe because ``_identity_match`` requires the
    two token sets to *contain* one another rather than merely to intersect (a
    single shared word like ``urgent`` is not enough to call two actions the
    same work), and because a containment match is only accepted when the extra
    tokens are derived from the shared ones via the stem/suffix rule. Containment
    alone is no longer sufficient after the derivation check was added.
    """
    lowered = token.lower()
    return len(token) >= 3 and lowered not in _WEAK_TOKENS and lowered not in _STOPWORDS


def identity_tokens(
    arguments: Mapping[str, Any] | None = None,
    *,
    volatile: Iterable[str] = (),
    external_id: str | None = None,
) -> frozenset[str]:
    """Distinctive resource tokens an operation refers to.

    Used by the ledger's defensive fallback when argument hashing cannot match
    two attempts that describe the same resource differently (field renames,
    path formatting). A token is a scalar value -- a string, or an integer
    rendered as one, since a row id of ``4821`` identifies a row as well as
    ``INV-001`` identifies an invoice (issue #36) -- plus the basename and
    basename-stem of any path-like value, and the same for ``external_id``.
    Weak tokens are dropped so the fallback never matches on incidental values
    like counts or status words.
    """
    tokens: set[str] = set()

    def collect(value: Any) -> None:
        # bool is an int subclass, and True/False name no resource.
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            tokens.add(str(value))
        elif isinstance(value, str):
            tokens.add(value)
            base = os.path.basename(value.rstrip("/\\"))
            stem, _ = os.path.splitext(base)
            if base != value:
                tokens.add(base)
            if stem and stem != base:
                tokens.add(stem)
        elif isinstance(value, Mapping):
            for v in value.values():
                collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)

    for v in _strip_volatile(arguments or {}, volatile).values():
        collect(v)
    if external_id:
        collect(external_id)

    return frozenset(t for t in tokens if _is_strong_token(t))


def leaf_tokens(tokens: frozenset[str]) -> frozenset[str]:
    """The tokens that name a resource rather than merely locate it.

    ``identity_tokens`` records a path-like value as the whole string *and* as
    the basename and stem derived from it. Only those leaves survive a change of
    rendering: an agent that writes ``/data/invoices/INV-5.pdf`` in one session
    and ``invoices/INV-5.pdf`` in the next means the same file, and the differing
    container would otherwise make the two look like different resources.

    Dropping the container is what lets ``_identity_match`` demand containment
    instead of a single shared token. On its own it also made two same-type
    actions on same-named files in *different* directories look like one, which
    silently swallowed the second side effect (issue #365). The container is
    therefore not discarded, only set aside: :func:`location_tokens` returns
    exactly what this drops, and ``_identity_match`` requires the locations to
    agree before it accepts a leaf-level match.
    """
    return frozenset(t for t in tokens if "/" not in t and "\\" not in t)


def location_tokens(tokens: frozenset[str]) -> frozenset[str]:
    """The path-like tokens :func:`leaf_tokens` sets aside.

    Its exact complement, so between them no token is lost. These say *where* a
    resource is, which is what distinguishes two files that happen to share a
    name (issue #365).
    """
    return frozenset(t for t in tokens if "/" in t or "\\" in t)


def _segments(path: str) -> list[str]:
    """A path split into comparable segments, separator-agnostic.

    Both separators are split on regardless of platform, because the token being
    compared was written by whatever machine recorded the action and need not
    match the one reading it.
    """
    normalized = os.path.normpath(os.path.expanduser(path)).replace("\\", "/")
    return [segment for segment in normalized.split("/") if segment not in ("", ".")]


def same_location(left: str, right: str) -> bool:
    """Whether two path-like tokens can be one file rendered differently.

    Drift makes a path more or less qualified about one resource, so the shorter
    rendering is a trailing part of the longer one: ``invoices/INV-5.pdf`` inside
    ``/data/invoices/INV-5.pdf``. Two paths that agree on nothing but the
    basename are different files, and treating them as one is what let
    ``/tenants/acme/report.csv`` swallow ``/tenants/globex/report.csv``
    (issue #365).

    Suffix comparison rather than equality, because that is the shape drift
    actually takes. Deliberately not a filesystem check: nothing here resolves
    against a working directory, so the answer is identical on every machine and
    for paths that no longer exist.
    """
    a, b = _segments(left), _segments(right)
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[-len(shorter) :] == shorter


def locations_agree(left: frozenset[str], right: frozenset[str]) -> bool:
    """Whether two token sets locate the same resource, or locate nothing.

    A side carrying no path-like token is making no claim about location, so it
    cannot contradict one: an argument rename that drops the path entirely still
    matches on its leaves. When both sides do locate something, at least one pair
    has to be reconcilable under :func:`same_location`.
    """
    if not left or not right:
        return True
    return any(same_location(a, b) for a in left for b in right)

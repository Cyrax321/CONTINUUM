"""Deterministic, canonical hashing for CONTINUUM state and events.

Canonicalization rules (stable across runs, platforms and hash inputs):

* Mapping keys are sorted recursively.
* ``Enum`` values serialize as their value.
* ``datetime`` serializes as ISO-8601 with a timezone offset.
* ``bytes`` serialize as base64.
* Floats must be finite (``NaN``/``Infinity`` are rejected) so hashes never
  differ bit-for-bit across platforms.
* Any object implementing ``model_dump(mode="json")`` is dumped to JSON-native
  types first.

The same object must always produce the same hash, regardless of key insertion
order or module import order.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

__all__ = ["canonical", "to_json", "stable_hash", "hash_content", "make_id"]


def _canonical(value: Any) -> Any:
    # Enums first: StrEnum/IntEnum are str/int subclasses and must serialize by value.
    if isinstance(value, Enum):
        return _canonical(value.value)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float cannot be hashed deterministically: {value!r}")
        return value
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (datetime, date)):
        dt = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        # Keys are coerced to strings so a value hashes the same before and
        # after a JSON round-trip. Collisions would silently drop data, so they
        # are an error rather than a surprise.
        canonical_items: dict[str, Any] = {}
        for key in sorted(value, key=str):
            name = str(key)
            if name in canonical_items:
                raise ValueError(f"ambiguous mapping key after string coercion: {name!r}")
            canonical_items[name] = _canonical(value[key])
        return canonical_items
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets have no stable order; convert to a sorted list before hashing")
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump(mode="json"))
    raise TypeError(f"cannot deterministically hash value of type {type(value).__name__}")


def canonical(value: Any) -> Any:
    """Return a canonical (sorted, JSON-native) representation of ``value``."""
    return _canonical(value)


def to_json(value: Any) -> str:
    """Serialize any value to canonical JSON with sorted keys and no spaces."""
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_content(raw: bytes, algorithm: str = "sha256") -> str:
    """Hash raw bytes and return a hex digest."""
    return hashlib.new(algorithm, raw).hexdigest()


def stable_hash(value: Any, algorithm: str = "sha256") -> str:
    """Return a deterministic content hash of ``value``."""
    return hash_content(to_json(value).encode("utf-8"), algorithm)


def make_id(prefix: str) -> str:
    """Random hex identifier with a human-readable prefix, e.g. ``run_8f3a...``.

    Uses 16 bytes (128 bits) of cryptographic randomness, the same budget as
    UUID v4, so birthday-paradox collisions are negligible even at billions of
    IDs.
    """
    return f"{prefix}_{secrets.token_hex(16)}"

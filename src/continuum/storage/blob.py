"""Out-of-band blob store for oversized event payloads (issues #254, #1418).

When event payloads exceed a configured byte threshold, storage engines offload
the payload bytes to content-addressed blob files and replace the inline payload
with an offload descriptor:

    {"__offloaded": sha256_hex, "size_bytes": length, "keys": list(payload.keys())}

The hash chain calculation continues to cover the offloaded descriptor,
preserving cryptographic tamper evidence across all events.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from continuum.events import IntegrityViolation
from continuum.security.hashing import to_json
from continuum.storage.base import CorruptedRecord, StorageError

__all__ = [
    "DEFAULT_PAYLOAD_OFFLOAD_BYTES",
    "OFFLOAD_KEY",
    "PAYLOAD_OFFLOAD_ENV_VAR",
    "audit_blob_descriptor",
    "create_offload_descriptor",
    "get_blob_dir",
    "get_blob_path",
    "get_payload_offload_threshold",
    "is_offload_descriptor",
    "load_blob_payload",
    "maybe_offload_payload",
    "maybe_rehydrate_payload",
    "read_blob",
    "write_blob",
]

PAYLOAD_OFFLOAD_ENV_VAR = "CONTINUUM_PAYLOAD_OFFLOAD_BYTES"
DEFAULT_PAYLOAD_OFFLOAD_BYTES = 0
OFFLOAD_KEY = "__offloaded"


def get_payload_offload_threshold() -> int:
    """Return the configured payload offload threshold in bytes (0 = disabled)."""
    raw = os.environ.get(PAYLOAD_OFFLOAD_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_PAYLOAD_OFFLOAD_BYTES
    try:
        val = int(raw)
        return max(val, 0)
    except (ValueError, TypeError):
        return DEFAULT_PAYLOAD_OFFLOAD_BYTES


def get_blob_dir(storage_dir: str | Path) -> Path:
    """Return the blob storage directory for a given base storage directory."""
    return Path(storage_dir) / "blobs"


def get_blob_path(storage_dir: str | Path, sha256_hex: str) -> Path:
    """Return the content-addressed path for a SHA-256 blob."""
    return get_blob_dir(storage_dir) / f"{sha256_hex.lower()}.blob"


def is_offload_descriptor(payload: Any) -> bool:
    """Check if a mapping is an offload reference descriptor."""
    if not isinstance(payload, Mapping):
        return False
    ref = payload.get(OFFLOAD_KEY)
    return isinstance(ref, str) and len(ref) == 64 and isinstance(payload.get("size_bytes"), int)


def create_offload_descriptor(
    sha256_hex: str, size_bytes: int, keys: Iterable[str]
) -> dict[str, Any]:
    """Create an offload reference descriptor dictionary."""
    return {
        OFFLOAD_KEY: sha256_hex.lower(),
        "size_bytes": size_bytes,
        "keys": list(keys),
    }


def write_blob(
    storage_dir: str | Path,
    canonical_bytes: bytes,
    sha256_hex: str | None = None,
) -> Path:
    """Write canonical JSON bytes to <storage_dir>/blobs/<sha256>.blob atomically."""
    if sha256_hex is None:
        sha256_hex = hashlib.sha256(canonical_bytes).hexdigest()
    else:
        sha256_hex = sha256_hex.lower()

    target_path = get_blob_path(storage_dir, sha256_hex)
    if target_path.exists():
        try:
            if (
                target_path.stat().st_size == len(canonical_bytes)
                and target_path.read_bytes() == canonical_bytes
            ):
                return target_path
        except OSError:
            pass

    blob_dir = target_path.parent
    try:
        blob_dir.mkdir(parents=True, exist_ok=True)
        tmp_name = f"{sha256_hex}.tmp.{os.getpid()}_{uuid.uuid4().hex}"
        tmp_path = blob_dir / tmp_name
        with tmp_path.open("wb") as f:
            f.write(canonical_bytes)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(target_path)
        return target_path
    except Exception as exc:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise StorageError(f"failed to write blob {sha256_hex}: {exc}") from exc


def maybe_offload_payload(
    payload: Mapping[str, Any] | None,
    *,
    storage_dir: str | Path,
    threshold: int | None = None,
) -> tuple[Mapping[str, Any], bool]:
    """Offload payload to blob storage if its canonical JSON exceeds threshold.

    Returns a tuple of (resulting_payload, was_offloaded). When threshold is 0
    or not exceeded, payload is returned unchanged and was_offloaded is False.
    """
    if payload is None:
        return {}, False
    if is_offload_descriptor(payload):
        return payload, False

    limit = threshold if threshold is not None else get_payload_offload_threshold()
    if limit <= 0:
        return payload, False

    canonical_json = to_json(payload)
    canonical_bytes = canonical_json.encode("utf-8")
    length = len(canonical_bytes)
    if length <= limit:
        return payload, False

    sha256_hex = hashlib.sha256(canonical_bytes).hexdigest()
    write_blob(storage_dir, canonical_bytes, sha256_hex=sha256_hex)
    descriptor = create_offload_descriptor(sha256_hex, length, payload.keys())
    return descriptor, True


def read_blob(storage_dir: str | Path, sha256_hex: str) -> bytes:
    """Read blob bytes from <storage_dir>/blobs/<sha256>.blob.

    Verifies the SHA-256 digest of the read bytes matches sha256_hex.
    Raises CorruptedRecord if missing, unreadable, or digest does not match.
    """
    blob_path = get_blob_path(storage_dir, sha256_hex)
    try:
        data = blob_path.read_bytes()
    except FileNotFoundError as exc:
        raise CorruptedRecord(f"missing blob {sha256_hex!r} at {blob_path}") from exc
    except OSError as exc:
        raise CorruptedRecord(f"unreadable blob {sha256_hex!r} at {blob_path}: {exc}") from exc

    actual_digest = hashlib.sha256(data).hexdigest()
    if actual_digest != sha256_hex.lower():
        raise CorruptedRecord(f"corrupted blob {sha256_hex!r}: computed digest {actual_digest!r}")
    return data


def load_blob_payload(storage_dir: str | Path, descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """Rehydrate a payload from its offload descriptor."""
    sha256_hex = descriptor.get(OFFLOAD_KEY)
    if not isinstance(sha256_hex, str):
        raise CorruptedRecord(f"invalid offload descriptor: missing {OFFLOAD_KEY}")
    data = read_blob(storage_dir, sha256_hex)
    try:
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict):
            raise CorruptedRecord(f"blob {sha256_hex} does not deserialize to a JSON object")
        return obj
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CorruptedRecord(f"blob {sha256_hex} failed to decode: {exc}") from exc


def maybe_rehydrate_payload(
    payload: Mapping[str, Any],
    *,
    storage_dir: str | Path,
) -> Mapping[str, Any]:
    """Rehydrate payload if it is an offload descriptor, otherwise return as-is."""
    if is_offload_descriptor(payload):
        return load_blob_payload(storage_dir, payload)
    return payload


def audit_blob_descriptor(
    storage_dir: str | Path,
    descriptor: Mapping[str, Any],
    *,
    run_id: str,
    sequence: int,
    event_id: str,
    prefix: str = "",
) -> IntegrityViolation | None:
    """Audit a single offload descriptor against disk.

    Returns an IntegrityViolation if the referenced blob is missing or corrupt,
    or None if the blob exists and its SHA-256 digest matches.
    """
    if not is_offload_descriptor(descriptor):
        return None

    sha256_hex = str(descriptor.get(OFFLOAD_KEY, "")).lower()
    blob_path = get_blob_path(storage_dir, sha256_hex)
    tag = f"{prefix}: " if prefix else ""

    if not blob_path.exists():
        return IntegrityViolation(
            kind="BLOB_MISSING",
            run_id=run_id,
            sequence=sequence,
            event_id=event_id,
            detail=f"{tag}missing blob {sha256_hex!r} at {blob_path}",
        )

    try:
        data = blob_path.read_bytes()
    except OSError as exc:
        return IntegrityViolation(
            kind="BLOB_MISSING",
            run_id=run_id,
            sequence=sequence,
            event_id=event_id,
            detail=f"{tag}unreadable blob {sha256_hex!r} at {blob_path}: {exc}",
        )

    computed = hashlib.sha256(data).hexdigest()
    if computed != sha256_hex:
        return IntegrityViolation(
            kind="BLOB_DIGEST_MISMATCH",
            run_id=run_id,
            sequence=sequence,
            event_id=event_id,
            detail=f"{tag}corrupted blob {sha256_hex!r}: computed digest {computed!r}",
        )

    expected_size = descriptor.get("size_bytes")
    if isinstance(expected_size, int) and len(data) != expected_size:
        return IntegrityViolation(
            kind="BLOB_DIGEST_MISMATCH",
            run_id=run_id,
            sequence=sequence,
            event_id=event_id,
            detail=f"{tag}corrupted blob {sha256_hex!r}: size {len(data)} != expected {expected_size}",
        )

    return None

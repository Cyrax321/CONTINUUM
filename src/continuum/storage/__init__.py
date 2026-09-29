"""Durable storage engines."""

from pathlib import Path

from continuum.storage.base import (
    CheckpointNotFound,
    ConcurrentWriteError,
    CorruptedRecord,
    RunNotFound,
    SchemaVersionError,
    Storage,
    StorageError,
)
from continuum.storage.blob import (
    DEFAULT_PAYLOAD_OFFLOAD_BYTES,
    OFFLOAD_KEY,
    PAYLOAD_OFFLOAD_ENV_VAR,
    audit_blob_descriptor,
    create_offload_descriptor,
    get_blob_dir,
    get_blob_path,
    get_payload_offload_threshold,
    is_offload_descriptor,
    load_blob_payload,
    maybe_offload_payload,
    maybe_rehydrate_payload,
    read_blob,
    write_blob,
)
from continuum.storage.sqlite import SQLiteStorage

__all__ = [
    "CheckpointNotFound",
    "ConcurrentWriteError",
    "CorruptedRecord",
    "DEFAULT_PAYLOAD_OFFLOAD_BYTES",
    "OFFLOAD_KEY",
    "PAYLOAD_OFFLOAD_ENV_VAR",
    "RunNotFound",
    "SQLiteStorage",
    "SchemaVersionError",
    "Storage",
    "StorageError",
    "audit_blob_descriptor",
    "create_offload_descriptor",
    "get_blob_dir",
    "get_blob_path",
    "get_payload_offload_threshold",
    "is_offload_descriptor",
    "load_blob_payload",
    "maybe_offload_payload",
    "maybe_rehydrate_payload",
    "open_storage",
    "read_blob",
    "write_blob",
]


def open_storage(
    url: str | Path = ":memory:",
    *,
    storage_dir: str | Path | None = None,
    payload_offload_bytes: int | None = None,
) -> Storage:
    """Open a storage engine from a URL.

    Supported: ``sqlite:///path.db`` (or a bare path / ``:memory:``) and
    ``postgresql://`` / ``postgres://`` (requires the ``[postgres]`` extra).
    An unrecognized scheme fails clearly rather than silently falling back.
    """
    raw = str(url)
    scheme = raw.split("://", 1)[0].lower() if "://" in raw else ""

    if scheme in ("postgres", "postgresql"):
        from continuum.storage.postgres import PostgresStorage

        return PostgresStorage(
            raw, storage_dir=storage_dir, payload_offload_bytes=payload_offload_bytes
        )
    if scheme not in ("", "sqlite"):
        raise ValueError(f"unsupported storage URL scheme: {scheme!r}")
    return SQLiteStorage(raw, storage_dir=storage_dir, payload_offload_bytes=payload_offload_bytes)

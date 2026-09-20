"""File content snapshots for dual-state rewind (issue #292)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

__all__ = ["snapshot_file", "restore_file", "snapshot_path", "MAX_SNAPSHOT_BYTES", "file_digest"]

MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024

_SNAPSHOT_DIR = Path(".continuum/file-snapshots")


def snapshot_path(sha256: str) -> Path:
    """Return the content-addressed snapshot path for a SHA-256 digest."""
    return _SNAPSHOT_DIR / sha256


def snapshot_file(path: str | Path, *, sha256: str | None = None) -> Path | None:
    """Snapshot a file, or return ``None`` when it is missing, oversized, or unreadable.

    The bytes are digested as they are copied, so a caller-supplied ``sha256``
    that does not match the content being written is refused with ``None``
    rather than filed under a digest it does not have (issue #1077): a racing
    writer gets a snapshot of nothing, not a snapshot that lies.
    """
    src = Path(path)
    try:
        stat = src.stat()
    except OSError:
        return None
    if stat.st_size > MAX_SNAPSHOT_BYTES:
        return None
    if sha256 is None:
        sha256 = file_digest(src)
        if sha256 is None:
            return None
    dst = snapshot_path(sha256)
    if dst.exists():
        return dst
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        digest = hashlib.sha256()
        with src.open("rb") as f, tmp.open("wb") as out:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != sha256:
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(dst)
        return dst
    except OSError:
        return None


def restore_file(path: str | Path, sha256: str) -> bool:
    """Restore a snapshot atomically, returning ``False`` when restoration fails."""
    src = snapshot_path(sha256)
    if not src.exists():
        return False
    dst = Path(path)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        return True
    except OSError:
        return False


def file_digest(path: str | Path) -> str | None:
    """Return a file's SHA-256 digest, or ``None`` when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None

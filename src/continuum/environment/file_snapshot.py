"""File content snapshots for dual-state rewind (issue #292)."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

__all__ = [
    "snapshot_file",
    "restore_file",
    "snapshot_path",
    "MAX_SNAPSHOT_BYTES",
    "file_digest",
    "is_content_digest",
]

MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024

_SNAPSHOT_DIR = Path(".continuum/file-snapshots")

# A SHA-256 hex digest: 64 lowercase hex characters and nothing else. The store
# is content-addressed, so the path *is* the digest -- anything else joined into
# it is a path, not a key (issue #1268).
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def is_content_digest(sha256: object) -> bool:
    """Return whether ``sha256`` is a literal SHA-256 hex digest."""
    return isinstance(sha256, str) and _DIGEST_RE.match(sha256) is not None


def snapshot_path(sha256: str) -> Path:
    """Return the content-addressed snapshot path for a SHA-256 digest.

    The store is content-addressed, so the digest *is* the path; a value that is
    not a 64-character hex digest is rejected rather than joined into the
    filesystem, where ``../`` would escape the snapshot directory (issue #1268).
    """
    if not is_content_digest(sha256):
        raise ValueError(f"not a SHA-256 digest: {sha256!r}")
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
    try:
        dst = snapshot_path(sha256)
    except ValueError:
        # A caller-supplied digest that is not a digest at all is refused with
        # None, like a mismatched one is, rather than raising (issue #1268).
        return None
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
    """Restore a snapshot atomically, returning ``False`` when restoration fails.

    A ``sha256`` that is not a content address also returns ``False``. This is
    the read side, where an unvalidated key is not a failed lookup but a copy of
    whatever the joined path happens to point at into the workspace (#1268), so
    it fails closed like any other restore failure rather than raising: the
    callers that can reach it are adapters and embedding applications, and the
    store's contract on every other failure path is a ``False`` they already
    handle. ``snapshot_path`` still rejects the value, so a caller that needs to
    tell a bad key from a missing one can pre-check ``is_content_digest``.
    """
    try:
        src = snapshot_path(sha256)
    except ValueError:
        return False
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

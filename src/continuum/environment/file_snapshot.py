"""File content snapshots for dual-state rewind (issue #292)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

__all__ = ["snapshot_file", "restore_file", "snapshot_path", "MAX_SNAPSHOT_BYTES", "file_digest"]

MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024

_SNAPSHOT_DIR = Path(".continuum/file-snapshots")


def snapshot_path(sha256: str) -> Path:
    return _SNAPSHOT_DIR / sha256


def snapshot_file(path: str | Path, *, sha256: str | None = None) -> Path | None:
    src = Path(path)
    try:
        stat = src.stat()
    except OSError:
        return None
    if stat.st_size > MAX_SNAPSHOT_BYTES:
        return None
    if sha256 is not None:
        dst = snapshot_path(sha256)
        if dst.exists():
            return dst
    if sha256 is None:
        digest = hashlib.sha256()
        try:
            with src.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
        except OSError:
            return None
        dst = snapshot_path(sha256)
        if dst.exists():
            return dst
    else:
        dst = snapshot_path(sha256)
        if dst.exists():
            return dst
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        return dst
    except OSError:
        return None


def restore_file(path: str | Path, sha256: str) -> bool:
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
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None

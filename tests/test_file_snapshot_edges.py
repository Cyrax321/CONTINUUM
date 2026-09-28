"""Direct tests for file snapshot boundary behavior (issue #902)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import continuum.environment.file_snapshot as file_snapshot


def test_snapshot_file_returns_none_for_missing_source(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")

    assert file_snapshot.snapshot_file(tmp_path / "missing.txt") is None


def test_snapshot_file_returns_none_without_writing_oversized_source(
    tmp_path: Path, monkeypatch: Any
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(file_snapshot, "MAX_SNAPSHOT_BYTES", 3)
    source = tmp_path / "large.txt"
    source.write_bytes(b"1234")

    assert file_snapshot.snapshot_file(source) is None
    assert not snapshot_dir.exists()


def test_snapshot_file_stored_content_matches_supplied_digest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    source = tmp_path / "f.txt"
    source.write_bytes(b"original content v1")
    digest = hashlib.sha256(b"original content v1").hexdigest()

    dst = file_snapshot.snapshot_file(source, sha256=digest)

    assert dst == snapshot_dir / digest
    assert hashlib.sha256(dst.read_bytes()).hexdigest() == digest
    # A repeat call deduplicates onto the same content-addressed path.
    assert file_snapshot.snapshot_file(source, sha256=digest) == dst


def test_snapshot_file_refuses_a_stale_supplied_digest(tmp_path: Path, monkeypatch: Any) -> None:
    # Issue #1077: the digest the caller computed and the bytes on disk at
    # copy time can differ when a writer races the snapshot. The mismatching
    # content must not be filed under the digest it does not have.
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    source = tmp_path / "f.txt"
    source.write_bytes(b"original content v1")
    stale = hashlib.sha256(b"original content v1").hexdigest()
    source.write_bytes(b"original content v1 with a racing append")

    assert file_snapshot.snapshot_file(source, sha256=stale) is None
    assert not (snapshot_dir / stale).exists()
    # No .tmp litter is left behind by the refused write.
    if snapshot_dir.exists():
        assert list(snapshot_dir.glob("*.tmp")) == []


def test_snapshot_file_without_digest_keys_by_content(tmp_path: Path, monkeypatch: Any) -> None:
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    source = tmp_path / "f.txt"
    source.write_bytes(b"some content")

    dst = file_snapshot.snapshot_file(source)

    assert dst is not None
    assert dst == snapshot_dir / hashlib.sha256(b"some content").hexdigest()


def test_restore_file_returns_false_for_absent_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")

    assert file_snapshot.restore_file(tmp_path / "restored.txt", "missing-sha") is False


def test_file_digest_returns_none_for_missing_source(tmp_path: Path) -> None:
    assert file_snapshot.file_digest(tmp_path / "missing.txt") is None


def test_snapshot_path_uses_file_snapshot_directory(tmp_path: Path, monkeypatch: Any) -> None:
    snapshot_dir = tmp_path / ".continuum" / "file-snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)

    assert file_snapshot.snapshot_path("abc123") == snapshot_dir / "abc123"

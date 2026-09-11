"""Direct tests for file snapshot boundary behavior (issue #902)."""

from __future__ import annotations

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


def test_restore_file_returns_false_for_absent_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", tmp_path / "snapshots")

    assert file_snapshot.restore_file(tmp_path / "restored.txt", "missing-sha") is False


def test_file_digest_returns_none_for_missing_source(tmp_path: Path) -> None:
    assert file_snapshot.file_digest(tmp_path / "missing.txt") is None


def test_snapshot_path_uses_file_snapshot_directory(tmp_path: Path, monkeypatch: Any) -> None:
    snapshot_dir = tmp_path / ".continuum" / "file-snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)

    assert file_snapshot.snapshot_path("abc123") == snapshot_dir / "abc123"

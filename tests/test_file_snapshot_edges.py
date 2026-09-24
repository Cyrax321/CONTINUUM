"""Direct tests for file snapshot boundary behavior (issue #902)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

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
    digest = hashlib.sha256(b"never snapshotted").hexdigest()

    assert file_snapshot.restore_file(tmp_path / "restored.txt", digest) is False


def test_file_digest_returns_none_for_missing_source(tmp_path: Path) -> None:
    assert file_snapshot.file_digest(tmp_path / "missing.txt") is None


def test_snapshot_path_uses_file_snapshot_directory(tmp_path: Path, monkeypatch: Any) -> None:
    snapshot_dir = tmp_path / ".continuum" / "file-snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    digest = hashlib.sha256(b"some content").hexdigest()

    assert file_snapshot.snapshot_path(digest) == snapshot_dir / digest


def test_snapshot_path_rejects_a_digest_that_is_not_a_digest(tmp_path: Path) -> None:
    # Issue #1268: the store is content-addressed, so anything joined into the
    # path that is not a 64-char hex digest is a path, not a key -- ``../``
    # escapes the snapshot directory and lands anywhere the process can reach.
    for bad in ("../../../secret/keyfile.txt", "abc123", "X" * 64, "0" * 63, "0" * 65, "", None):
        with pytest.raises(ValueError):
            file_snapshot.snapshot_path(bad)


def test_is_content_digest_accepts_only_a_lowercase_hex_digest() -> None:
    assert file_snapshot.is_content_digest(hashlib.sha256(b"x").hexdigest()) is True
    assert file_snapshot.is_content_digest("../../../secret/keyfile.txt") is False
    assert file_snapshot.is_content_digest(hashlib.sha256(b"x").hexdigest().upper()) is False
    assert file_snapshot.is_content_digest(None) is False


def test_snapshot_file_refuses_a_supplied_digest_that_is_not_a_digest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # A caller that recorded a digest it did not compute itself gets None, the
    # same refusal a mismatched digest already earns, rather than a raise.
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    source = tmp_path / "f.txt"
    source.write_bytes(b"content")

    assert file_snapshot.snapshot_file(source, sha256="../../../secret/keyfile.txt") is None
    assert not snapshot_dir.exists()


def test_restore_file_will_not_copy_a_file_outside_the_store(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The read side is the worse direction: restore_file copies whatever the
    # key resolves to over a workspace file. A traversal key must not resolve,
    # and it fails closed to False -- the contract every other restore failure
    # already honours -- so a caller that does not catch ValueError still cannot
    # reach copyfile with a path the store never wrote (issue #1268).
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setattr(file_snapshot, "_SNAPSHOT_DIR", snapshot_dir)
    secret = tmp_path / "secret" / "keyfile.txt"
    secret.parent.mkdir()
    secret.write_text("TOP_SECRET")
    workspace = tmp_path / "workspace" / "leaked.txt"
    workspace.parent.mkdir()
    traversal = "../secret/keyfile.txt"

    assert file_snapshot.restore_file(workspace, traversal) is False
    assert not workspace.exists()
    # The traversal target is untouched as well as unread.
    assert secret.read_text() == "TOP_SECRET"

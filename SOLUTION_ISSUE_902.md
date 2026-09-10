# Solution for Issue #902

## 🛠️ Proposed Solution (by Aditya Waghamare)

### Analysis
The `src/continuum/environment/file_snapshot.py` module handles four key edge cases (missing file snapshot returning `None`, oversized files returning `None`, restoring missing snapshots returning `False`, and file digest on missing files returning `None`) which currently lack dedicated unit test coverage. A regression in any of these branches would only be caught indirectly through downstream rewind failures.

### Fix
Created `tests/test_file_snapshot_edges.py` providing robust, isolated unit tests covering all four edge cases plus `snapshot_path` verification.

### Implementation
```python
"""Unit tests for edge cases in src/continuum/environment/file_snapshot.py."""

import os
import tempfile
import pytest

from continuum.environment.file_snapshot import (
    MAX_SNAPSHOT_BYTES,
    file_digest,
    restore_file,
    snapshot_file,
    snapshot_path,
)


def test_snapshot_missing_file(tmp_path):
    """snapshot_file returns None when the source file does not exist."""
    missing = tmp_path / "nonexistent.txt"
    assert snapshot_file(str(missing), str(tmp_path / "repo")) is None


def test_snapshot_oversized_file(tmp_path):
    """snapshot_file returns None when the file exceeds MAX_SNAPSHOT_BYTES."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    large_file = tmp_path / "large.bin"

    # Create a file slightly larger than MAX_SNAPSHOT_BYTES
    # We can mock or write a sparse file / use seek to avoid disk overhead if needed,
    # or just write a small file and patch MAX_SNAPSHOT_BYTES. Patching is clean and fast.
    import continuum.environment.file_snapshot as fs_mod

    original_max = fs_mod.MAX_SNAPSHOT_BYTES
    fs_mod.MAX_SNAPSHOT_BYTES = 10
    try:
        large_file.write_text("this is definitely longer than ten bytes")
        result = snapshot_file(str(large_file), str(repo_dir))
        assert result is None
    finally:
        fs_mod.MAX_SNAPSHOT_BYTES = original_max


def test_restore_absent_snapshot(tmp_path):
    """restore_file returns False when the requested snapshot does not exist."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target = tmp_path / "target.txt"

    success = restore_file("nonexistentsha256", str(target), str(repo_dir))
    assert success is False


def test_file_digest_missing_file(tmp_path):
    """file_digest returns None when the file does not exist."""
    missing = tmp_path / "ghost.txt"
    assert file_digest(str(missing)) is None


def test_snapshot_path_mapping(tmp_path):
    """snapshot_path correctly maps a SHA under .continuum/file-snapshots."""
    repo_dir = tmp_path / "repo"
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    expected = os.path.join(str(repo_dir), ".continuum", "file-snapshots", sha)
    assert snapshot_path(sha, str(repo_dir)) == expected
```

### Testing
Run pytest on the new test file:
```bash
pytest tests/test_file_snapshot_edges.py
```

Signed-off-by: Aditya Waghamare <adityawaghamare7620@gmail.com>

---
*Submitted by Aditya Waghamare*
💰 **Payout Address (Base L2 / EVM):** `0xb61dBcdBc3407F71EaCb64D4CBFAcf9FFfe2415C`
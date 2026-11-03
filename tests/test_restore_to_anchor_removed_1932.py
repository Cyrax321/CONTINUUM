from __future__ import annotations

import continuum.recovery.restore as restore


def test_restore_to_anchor_not_present_1932() -> None:
    """Issue #1932: ensure dead restore_to_anchor wrapper is removed and not resurrected."""
    assert not hasattr(restore, "restore_to_anchor")
    assert "restore_to_anchor" not in getattr(restore, "__all__", [])

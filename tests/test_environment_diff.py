"""Direct coverage of EnvironmentDiff, ResourceDelta, and diff_environments (#1991).

The high-level environment tests exercise ``diff_environments`` through
snapshot capture, but the delta properties, rendering branches, and edge
cases are never asserted on their own: recovery downgrades on UNKNOWN rather
than treating it as a softer UNCHANGED, so the classification and the strings
it renders are behaviour, not cosmetics.
"""

from __future__ import annotations

import pytest

from continuum.environment.diff import (
    EnvironmentDiff,
    ResourceChange,
    ResourceDelta,
    diff_environments,
)
from continuum.environment.snapshot import UNKNOWN_VERSION
from continuum.models import EnvironmentSnapshot, EnvResource


def _snapshot(resources: dict[str, EnvResource], run_id: str = "run_1") -> EnvironmentSnapshot:
    return EnvironmentSnapshot(run_id=run_id, resources=resources)


def _resource(
    name: str,
    *,
    version: str | None = "v1",
    checksum: str | None = None,
    metadata: dict[str, str] | None = None,
) -> EnvResource:
    return EnvResource(
        name=name,
        kind="resource",
        version=version,
        checksum=checksum,
        metadata=metadata or {},
    )


# --- ResourceDelta ----------------------------------------------------------- #


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (ResourceChange.CHANGED, True),
        (ResourceChange.REMOVED, True),
        (ResourceChange.UNKNOWN, True),
        (ResourceChange.UNCHANGED, False),
        (ResourceChange.ADDED, False),
    ],
)
def test_resource_delta_breaking_classification(change: ResourceChange, expected: bool) -> None:
    """CHANGED, REMOVED and UNKNOWN can invalidate dependent work; UNCHANGED and
    ADDED cannot. UNKNOWN is breaking on purpose: a resource that could not be
    inspected must not read as intact."""
    delta = ResourceDelta(resource="db", change=change)
    assert delta.breaking is expected


def test_resource_delta_renders_every_change_variant() -> None:
    """Each of the five classifications has its own human-readable line, so an
    operator reading a rendered diff can tell a verification gap from a
    break."""
    assert (
        ResourceDelta(
            resource="db", change=ResourceChange.CHANGED, before="v1", after="v2"
        ).render()
        == "db: v1 -> v2"
    )
    assert (
        ResourceDelta(resource="db", change=ResourceChange.REMOVED, before="v1").render()
        == "db: removed (was v1)"
    )
    assert (
        ResourceDelta(resource="db", change=ResourceChange.ADDED, after="v2").render()
        == "db: added (v2)"
    )
    assert (
        ResourceDelta(
            resource="db", change=ResourceChange.UNKNOWN, detail="connection refused"
        ).render()
        == "db: could not be verified (connection refused)"
    )
    assert ResourceDelta(resource="db", change=ResourceChange.UNCHANGED).render() == "db: unchanged"


# --- EnvironmentDiff --------------------------------------------------------- #


def _mixed_diff() -> EnvironmentDiff:
    return EnvironmentDiff(
        deltas=[
            ResourceDelta(resource="same", change=ResourceChange.UNCHANGED, before="c", after="c"),
            ResourceDelta(
                resource="modified", change=ResourceChange.CHANGED, before="a", after="b"
            ),
            ResourceDelta(resource="gone", change=ResourceChange.REMOVED, before="a"),
            ResourceDelta(resource="mystery", change=ResourceChange.UNKNOWN, detail="timed out"),
            ResourceDelta(resource="fresh", change=ResourceChange.ADDED, after="b"),
        ]
    )


def test_environment_diff_filtered_subsets() -> None:
    """changed / unknown / breaking partition the deltas by what recovery is
    allowed to conclude, and stable is their negation."""
    diff = _mixed_diff()
    assert [d.resource for d in diff.changed] == ["modified"]
    assert [d.resource for d in diff.unknown] == ["mystery"]
    # CHANGED, REMOVED and UNKNOWN are all breaking; ADDED and UNCHANGED are not.
    assert [d.resource for d in diff.breaking] == ["modified", "gone", "mystery"]
    assert diff.stable is False
    empty = EnvironmentDiff(deltas=[])
    assert empty.breaking == ()
    assert empty.stable is True


def test_environment_diff_for_resource_lookup() -> None:
    """for_resource finds the first delta for a name and returns None for a name
    the diff says nothing about."""
    diff = _mixed_diff()
    assert diff.for_resource("mystery") is not None
    assert diff.for_resource("mystery").detail == "timed out"
    assert diff.for_resource("never-captured") is None


def test_environment_diff_render_empty() -> None:
    """An empty diff renders as absent data, never as 'unchanged': absence of
    evidence is not evidence of stability."""
    assert EnvironmentDiff().render() == "No environment data to compare."


def test_environment_diff_render_all_unchanged() -> None:
    """A diff where every resource verified identical reports the count, which
    is what a reader needs to know the sweep was complete."""
    diff = EnvironmentDiff(
        deltas=[
            ResourceDelta(resource="a", change=ResourceChange.UNCHANGED, before="c", after="c"),
            ResourceDelta(resource="b", change=ResourceChange.UNCHANGED, before="d", after="d"),
        ]
    )
    assert diff.render() == "Environment unchanged (2 resources verified)."


def test_environment_diff_render_lists_interesting_deltas() -> None:
    """Only non-unchanged deltas are listed, indented two spaces, so a rendered
    report leads with what changed or could not be verified."""
    rendered = _mixed_diff().render()
    assert "same: unchanged" not in rendered
    assert rendered == (
        "  modified: a -> b\n"
        "  gone: removed (was a)\n"
        "  mystery: could not be verified (timed out)\n"
        "  fresh: added (b)"
    )


# --- diff_environments ------------------------------------------------------- #


def test_missing_snapshot_on_either_side_yields_an_empty_diff() -> None:
    """A missing before or after snapshot returns an empty diff rather than a
    false 'unchanged', so callers must consult ``deltas`` before concluding
    anything."""
    after = _snapshot({"db": _resource("db")})
    before = _snapshot({"db": _resource("db")})
    assert diff_environments(None, after) == EnvironmentDiff()
    assert diff_environments(before, None) == EnvironmentDiff()
    assert diff_environments(None, None) == EnvironmentDiff()


def test_added_resource_is_reported_as_added() -> None:
    before = _snapshot({"db": _resource("db")})
    after = _snapshot({"db": _resource("db"), "cache": _resource("cache", version="v2")})
    diff = diff_environments(before, after)
    added = diff.for_resource("cache")
    assert added is not None
    assert added.change is ResourceChange.ADDED
    assert added.after == "v2"
    # An addition does not break the environment.
    assert added.breaking is False
    assert diff.stable is True


def test_removed_resource_is_reported_with_detail() -> None:
    before = _snapshot({"db": _resource("db", version="v1"), "cache": _resource("cache")})
    after = _snapshot({"db": _resource("db", version="v1")})
    diff = diff_environments(before, after)
    removed = diff.for_resource("cache")
    assert removed is not None
    assert removed.change is ResourceChange.REMOVED
    assert removed.before == "v1"
    assert removed.detail == "resource is no longer present in the environment"
    assert removed.breaking is True


def test_unverifiable_resource_reads_its_reason_from_metadata() -> None:
    """A resource that could not be inspected is UNKNOWN, and the detail stops
    being generic when the snapshot recorded why: the error wins over the
    skipped reason, and the current side wins over the previous one."""
    before = _snapshot(
        {
            "api": _resource(
                "api", version=UNKNOWN_VERSION, metadata={"skipped": "probe disabled"}
            ),
            "other": _resource("other", version=UNKNOWN_VERSION, metadata={"error": "stale"}),
        }
    )
    after = _snapshot(
        {
            "api": _resource(
                "api", version=UNKNOWN_VERSION, metadata={"error": "connection refused"}
            ),
            "other": _resource("other", version=UNKNOWN_VERSION, metadata={"error": "stale"}),
        }
    )
    diff = diff_environments(before, after)
    api = diff.for_resource("api")
    assert api is not None
    assert api.change is ResourceChange.UNKNOWN
    # The error on the current side wins over the skipped reason on the old one.
    assert api.detail == "connection refused"
    assert api.breaking is True
    other = diff.for_resource("other")
    assert other is not None
    # Both sides are unverifiable, so both land in the unknown subset, in the
    # sorted delta order.
    assert diff.unknown == (api, other)
    # A second delta keeps the previous side's reason when neither current key
    # is present: 'stale' comes from before, not the fallback string.
    assert other.detail == "stale"


def test_unverifiable_resource_without_recorded_reason_falls_back() -> None:
    """With no error or skipped reason on either side, the detail still names
    the failure instead of being empty."""
    before = _snapshot({"api": _resource("api", version=UNKNOWN_VERSION)})
    after = _snapshot({"api": _resource("api", version=UNKNOWN_VERSION)})
    diff = diff_environments(before, after)
    api = diff.for_resource("api")
    assert api is not None
    assert api.change is ResourceChange.UNKNOWN
    assert api.detail == "resource could not be inspected"


def test_resource_with_no_version_on_either_side_is_unknown() -> None:
    """Neither side recorded a version or checksum: the comparison cannot
    conclude anything, so it is UNKNOWN with an explicit detail, not UNCHANGED."""
    before = _snapshot({"tool": _resource("tool", version=None)})
    after = _snapshot({"tool": _resource("tool", version=None)})
    diff = diff_environments(before, after)
    delta = diff.for_resource("tool")
    assert delta is not None
    assert delta.change is ResourceChange.UNKNOWN
    assert delta.detail == "no version or checksum recorded on either side"
    assert delta.breaking is True


def test_checksum_is_preferred_over_version() -> None:
    """A checksum is the strongest identity, so it is what the comparison uses:
    even identical versions cannot fake identity when the checksums differ."""
    before = _snapshot(
        {
            "tool": _resource("tool", version="v1", checksum="sha-aaa"),
            "sibling": _resource("sibling"),
        }
    )
    after = _snapshot(
        {
            "tool": _resource("tool", version="v1", checksum="sha-bbb"),
            # Version-only entry on the previous side: the checksum of the new
            # side is compared against the old version, so they differ.
            "sibling": _resource("sibling", version="v1", checksum="sha-ccc"),
        }
    )
    diff = diff_environments(before, after)
    tool = diff.for_resource("tool")
    assert tool is not None
    assert tool.change is ResourceChange.CHANGED
    assert tool.before == "sha-aaa"
    assert tool.after == "sha-bbb"
    # 'sibling' carried no checksum before, so _version_of used its version,
    # which does not equal the new checksum.
    sibling = diff.for_resource("sibling")
    assert sibling is not None
    assert sibling.change is ResourceChange.CHANGED


def test_identical_checksums_verify_unchanged() -> None:
    """Two snapshots that agree on the checksum of every resource verify the
    environment unchanged, and the diff renders as such."""
    before = _snapshot(
        {
            "tool": _resource("tool", version="v1", checksum="sha-aaa"),
            "api": _resource("api", version="2.1", checksum="sha-bbb"),
        }
    )
    after = _snapshot(
        {
            "tool": _resource("tool", version="v1", checksum="sha-aaa"),
            "api": _resource("api", version="2.1", checksum="sha-bbb"),
        }
    )
    diff = diff_environments(before, after)
    assert all(d.change is ResourceChange.UNCHANGED for d in diff.deltas)
    assert diff.changed == ()
    assert diff.breaking == ()
    assert diff.stable is True
    assert diff.render() == "Environment unchanged (2 resources verified)."


def test_checksum_difference_outweighs_equal_versions() -> None:
    """Same version, different checksum: the resource verifiably changed, which
    is the case a version-only stamp would have called unchanged (#738)."""
    before = _snapshot({"tool": _resource("tool", version="v1", checksum="sha-aaa")})
    after = _snapshot({"tool": _resource("tool", version="v1", checksum="sha-bbb")})
    diff = diff_environments(before, after)
    delta = diff.for_resource("tool")
    assert delta is not None
    assert delta.change is ResourceChange.CHANGED
    assert diff.changed == (delta,)
    assert diff.render() == "  tool: sha-aaa -> sha-bbb"


def test_versions_differ_when_no_checksum_is_recorded() -> None:
    """Without checksums the version is the identity, so a changed version is a
    verified change."""
    before = _snapshot({"tool": _resource("tool", version="v1")})
    after = _snapshot({"tool": _resource("tool", version="v2")})
    diff = diff_environments(before, after)
    delta = diff.for_resource("tool")
    assert delta is not None
    assert delta.change is ResourceChange.CHANGED
    assert (delta.before, delta.after) == ("v1", "v2")


def test_resources_are_walked_in_sorted_order() -> None:
    """The delta list is sorted by resource name, so a rendered diff is stable
    across insertion order and a reader sees a predictable sequence."""
    before = _snapshot({"zeta": _resource("zeta"), "alpha": _resource("alpha")})
    after = _snapshot({"zeta": _resource("zeta", version="v9"), "alpha": _resource("alpha")})
    diff = diff_environments(before, after)
    assert [d.resource for d in diff.deltas] == ["alpha", "zeta"]


def test_mixed_environment_classifies_every_resource_once() -> None:
    """One walk of the union of resource names: each resource lands in exactly
    one category, added on the new side, removed on the old, compared otherwise."""
    before = _snapshot(
        {
            "db": _resource("db", version="v1"),
            "cache": _resource("cache", version="v1"),
            "gone": _resource("gone", version="v1"),
        }
    )
    after = _snapshot(
        {
            "db": _resource("db", version="v2"),
            "cache": _resource("cache", version="v1"),
            "fresh": _resource("fresh", version="v1"),
        }
    )
    diff = diff_environments(before, after)
    kinds = {d.resource: d.change for d in diff.deltas}
    assert kinds == {
        "db": ResourceChange.CHANGED,
        "cache": ResourceChange.UNCHANGED,
        "gone": ResourceChange.REMOVED,
        "fresh": ResourceChange.ADDED,
    }
    assert diff.stable is False
    # Deltas are sorted by name, so the rendered order is cache, db, fresh,
    # gone with the unchanged entry dropped.
    assert diff.render() == ("  db: v1 -> v2\n  fresh: added (v1)\n  gone: removed (was v1)")

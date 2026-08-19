from __future__ import annotations

from pathlib import Path

from continuum.environment import (
    UNKNOWN_VERSION,
    CallableProvider,
    FileProvider,
    ResourceChange,
    StaticProvider,
    ValueProvider,
    capture,
    diff_environments,
    process_fingerprint,
)
from continuum.models import EnvResource

# --- capture --------------------------------------------------------------- #


def test_static_provider_records_what_it_was_given() -> None:
    snapshot = capture("run_1", StaticProvider(dataset="v3", model="gpt-x"))
    assert snapshot.resources["dataset"].version == "v3"
    assert snapshot.integrity_hash is not None


def test_capture_layers_providers_in_order() -> None:
    snapshot = capture(
        "run_1",
        [StaticProvider(dataset="v3"), StaticProvider(dataset="v4")],
    )
    assert snapshot.resources["dataset"].version == "v4"


def test_capture_accepts_a_single_provider_or_extras() -> None:
    snapshot = capture(
        "run_1",
        StaticProvider(dataset="v3"),
        extra={"manual": EnvResource(name="manual", version="1")},
    )
    assert set(snapshot.resources) == {"dataset", "manual"}


def test_an_empty_capture_is_valid() -> None:
    assert capture("run_1").resources == {}


def test_the_same_environment_hashes_identically() -> None:
    a = capture("run_1", StaticProvider(dataset="v3"))
    b = capture("run_1", StaticProvider(dataset="v3"))
    assert a.integrity_hash == b.integrity_hash


def test_files_are_fingerprinted_by_content_not_mtime(tmp_path: Path) -> None:
    """Touching a file must not invalidate work; changing it must."""
    target = tmp_path / "data.csv"
    target.write_text("alpha")

    first = capture("run_1", FileProvider([target]))
    target.touch()
    second = capture("run_1", FileProvider([target]))
    assert diff_environments(first, second).stable

    target.write_text("beta")
    third = capture("run_1", FileProvider([target]))
    assert not diff_environments(first, third).stable


def test_a_missing_file_is_omitted_not_raised(tmp_path: Path) -> None:
    """Issue #30: absence is reported by omission, so the diff reads 'removed'.

    Recording the file with ``version=None`` instead made a deleted file look
    like a *changed* resource, which understates what happened to it.
    """
    gone = tmp_path / "gone.csv"
    snapshot = capture("run_1", FileProvider([gone]))
    assert str(gone) not in snapshot.resources


def test_a_deleted_file_diffs_as_removed(tmp_path: Path) -> None:
    target = tmp_path / "data.csv"
    target.write_text("alpha")
    before = capture("run_1", FileProvider([target]))

    target.unlink()
    after = capture("run_1", FileProvider([target]))

    diff = diff_environments(before, after)
    assert not diff.stable
    delta = next(d for d in diff.deltas if d.resource == str(target))
    assert delta.change is ResourceChange.REMOVED


def test_an_unreadable_file_becomes_unknown(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    resource = capture("run_1", FileProvider([blocked])).resources[str(blocked)]
    assert resource.version == UNKNOWN_VERSION
    assert "error" in resource.metadata


def test_large_files_are_sized_rather_than_hashed(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 5000)
    resource = capture("run_1", FileProvider([big], max_bytes=1000)).resources[str(big)]
    assert resource.version == "size:5000"
    assert resource.metadata["skipped"]


def test_value_provider_fingerprints_in_memory_state() -> None:
    snapshot = capture("run_1", ValueProvider(config={"mode": "strict", "retries": 3}))
    assert snapshot.resources["config"].checksum is not None


def test_an_unhashable_value_becomes_unknown() -> None:
    resource = capture("run_1", ValueProvider(bad={1, 2})).resources["bad"]
    assert resource.version == UNKNOWN_VERSION
    assert "error" in resource.metadata


def test_a_probe_that_fails_becomes_unknown_not_an_exception() -> None:
    def unreachable() -> str:
        raise ConnectionError("api down")

    resource = capture("run_1", CallableProvider({"api": unreachable})).resources["api"]
    assert resource.version == UNKNOWN_VERSION
    assert "ConnectionError" in resource.metadata["error"]


def test_a_probe_can_return_a_value_or_a_resource() -> None:
    snapshot = capture(
        "run_1",
        CallableProvider(
            {
                "dataset": lambda: "v4",
                "session": lambda: EnvResource(name="session", version="active"),
                "absent": lambda: None,
            }
        ),
    )
    assert snapshot.resources["dataset"].version == "v4"
    assert snapshot.resources["session"].version == "active"
    assert snapshot.resources["absent"].version is None


def test_the_process_fingerprint_is_portable() -> None:
    fingerprint = process_fingerprint()
    assert {"python", "platform", "cwd"} <= set(fingerprint)


# --- diff ------------------------------------------------------------------ #


def test_identical_snapshots_are_stable() -> None:
    before = capture("run_1", StaticProvider(dataset="v3", api="live"))
    after = capture("run_1", StaticProvider(dataset="v3", api="live"))
    diff = diff_environments(before, after)
    assert diff.stable
    assert not diff.breaking
    assert "unchanged" in diff.render()


def test_a_version_change_is_breaking() -> None:
    diff = diff_environments(
        capture("run_1", StaticProvider(dataset="v3")),
        capture("run_1", StaticProvider(dataset="v4")),
    )
    assert not diff.stable
    delta = diff.for_resource("dataset")
    assert delta is not None
    assert delta.change is ResourceChange.CHANGED
    assert delta.before == "v3" and delta.after == "v4"
    assert "dataset: v3 -> v4" in diff.render()


def test_a_removed_resource_is_breaking() -> None:
    diff = diff_environments(
        capture("run_1", StaticProvider(dataset="v3", api="live")),
        capture("run_1", StaticProvider(dataset="v3")),
    )
    delta = diff.for_resource("api")
    assert delta is not None and delta.change is ResourceChange.REMOVED
    assert delta.breaking


def test_a_new_resource_is_not_breaking() -> None:
    """Adding something cannot invalidate work that never depended on it."""
    diff = diff_environments(
        capture("run_1", StaticProvider(dataset="v3")),
        capture("run_1", StaticProvider(dataset="v3", cache="warm")),
    )
    added = diff.for_resource("cache")
    assert added is not None and added.change is ResourceChange.ADDED
    assert not added.breaking
    assert diff.stable


def test_unknown_is_not_treated_as_unchanged() -> None:
    """The core conservatism: uncertainty must not read as safety."""
    before = capture("run_1", StaticProvider(api="live"))
    after = capture(
        "run_1",
        CallableProvider({"api": lambda: (_ for _ in ()).throw(TimeoutError("no answer"))}),
    )
    diff = diff_environments(before, after)

    delta = diff.for_resource("api")
    assert delta is not None
    assert delta.change is ResourceChange.UNKNOWN
    assert delta.breaking
    assert not diff.stable
    assert "could not be verified" in diff.render()


def test_a_resource_with_no_identity_on_either_side_is_unknown() -> None:
    nothing = EnvResource(name="mystery")
    diff = diff_environments(
        capture("run_1", extra={"mystery": nothing}),
        capture("run_1", extra={"mystery": nothing}),
    )
    delta = diff.for_resource("mystery")
    assert delta is not None and delta.change is ResourceChange.UNKNOWN


def test_a_checksum_outranks_a_version_label() -> None:
    """Same label, different content: the checksum must win."""
    before = capture("run_1", extra={"f": EnvResource(name="f", version="v1", checksum="aaa")})
    after = capture("run_1", extra={"f": EnvResource(name="f", version="v1", checksum="bbb")})
    assert diff_environments(before, after).changed


def test_a_missing_snapshot_yields_no_false_confidence() -> None:
    snapshot = capture("run_1", StaticProvider(dataset="v3"))
    assert diff_environments(None, snapshot).deltas == []
    assert diff_environments(snapshot, None).deltas == []
    assert "No environment data" in diff_environments(None, None).render()


def test_an_unchanged_delta_renders_on_its_own() -> None:
    """The summary hides unchanged resources, but the delta can still be shown."""
    before = capture("run_1", StaticProvider(dataset="v3"))
    after = capture("run_1", StaticProvider(dataset="v3"))
    delta = diff_environments(before, after).for_resource("dataset")
    assert delta is not None
    assert delta.render() == "dataset: unchanged"
    assert not delta.breaking


def test_every_change_kind_renders_readably() -> None:
    before = capture("run_1", StaticProvider(gone="v1", same="v2"))
    after = capture("run_1", StaticProvider(same="v2", fresh="v3"))
    rendered = diff_environments(before, after).render()
    assert "gone: removed (was v1)" in rendered
    assert "fresh: added (v3)" in rendered
    assert "same" not in rendered  # unchanged resources are not noise


def test_unknown_and_changed_are_reported_separately() -> None:
    before = capture("run_1", StaticProvider(dataset="v3", api="live"))
    after = capture(
        "run_1",
        [
            StaticProvider(dataset="v4"),
            CallableProvider({"api": lambda: (_ for _ in ()).throw(OSError("down"))}),
        ],
    )
    diff = diff_environments(before, after)
    assert len(diff.changed) == 1
    assert len(diff.unknown) == 1
    assert len(diff.breaking) == 2

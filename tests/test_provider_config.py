"""Configured, discoverable environment providers for resume-time revalidation.

Covers issue #762: a run records which world-observers it trusts, resume
resolves them and feeds the capture to the validator that already existed, and
every failure a provider can produce leaves its declared resources reported
unknown instead of quietly absent.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from continuum.checkpoint import CheckpointManager
from continuum.cli import main
from continuum.environment import (
    UNKNOWN_VERSION,
    CallableProvider,
    EnvironmentProvider,
    FileProvider,
    ProviderConfig,
    ProviderRegistry,
    ProviderSpec,
    ProviderStatus,
    StaticProvider,
    capture,
    config_from_events,
    record_provider_config,
    resolve_and_capture,
)
from continuum.events import EventType
from continuum.models import Run, StateStatus
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage

_BLOCKED = (StateStatus.UNKNOWN, StateStatus.CONFLICTED, StateStatus.INVALID)


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
    storage.append_event(
        "run_1", EventType.RUN_STARTED, {"goal": "Analyze 100 documents", "total": 100}
    )
    yield storage
    storage.close()


def seed(store: SQLiteStorage, dependency: str = "dataset", version: str = "v3") -> None:
    """A run that declared an external dependency and checkpointed against it.

    The checkpoint records the dependency's old version, so a resume that
    observes a different one has something to compare against.
    """
    store.append_event(
        "run_1", EventType.DEPENDENCY_DECLARED, {"resource": dependency, "version": version}
    )
    store.append_event(
        "run_1",
        EventType.EVIDENCE_ADDED,
        {"evidence_id": "e1", "summary": "s", "source": dependency},
    )
    for i in range(12):
        store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": i})
    CheckpointManager(store).checkpoint(
        "run_1", environment=capture("run_1", StaticProvider(**{dependency: version}))
    )


def configure(store: SQLiteStorage, *specs: ProviderSpec) -> ProviderConfig:
    config = ProviderConfig(specs=specs)
    record_provider_config(store, "run_1", config, provenance="test")
    return config


def dependency_entry(decision: object, resource: str) -> object:
    return next(
        e
        for e in decision.validation.report.statuses
        if e.component_id == resource  # type: ignore[attr-defined]
    )


# --- the configuration model ------------------------------------------------ #


def test_a_provider_name_is_an_identifier_not_a_path_or_module() -> None:
    """A name is a lookup key in a table the host controls, never a path."""
    with pytest.raises(ValueError, match="plain identifier"):
        ProviderSpec(provider="../continuum/plugins/x", resources={"x"})


def test_a_spec_refuses_to_record_a_secret_parameter() -> None:
    """A secret never reaches the hashed event log in the first place."""
    with pytest.raises(ValueError, match="secret"):
        ProviderSpec(provider="static", params={"api_key": "abc"}, resources={"x"})


def test_a_spec_refuses_a_parameter_that_cannot_be_serialized() -> None:
    """A callable would mean different things before and after a restart."""
    with pytest.raises(ValueError, match="JSON-native"):
        ProviderSpec(provider="static", params={"probe": lambda: 1})


def test_an_unknown_schema_version_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        ProviderConfig(schema_version=99)


def test_a_malformed_recorded_payload_is_skipped_not_trusted(store: SQLiteStorage) -> None:
    """A broken configuration event cannot silence a later, well-formed one."""
    store.append_event(
        "run_1",
        EventType.ENVIRONMENT_PROVIDERS_CONFIGURED,
        {"providers": [{"provider": "../x", "resources": ["a"]}], "schema_version": 1},
    )
    configure(store, ProviderSpec(provider="static", params={"resources": {"a": "v1"}}))
    config = config_from_events(store.read_all_events("run_1"))
    assert config is not None
    assert [s.provider for s in config.specs] == ["static"]


def test_a_run_that_never_configured_anything_reads_as_none(store: SQLiteStorage) -> None:
    assert config_from_events(store.read_all_events("run_1")) is None


# --- file and git providers ------------------------------------------------- #


def test_a_configured_file_provider_fingerprints_the_declared_file(
    store: SQLiteStorage, tmp_path: Path
) -> None:
    data = tmp_path / "data.csv"
    data.write_text("alpha")
    config = configure(store, ProviderSpec(provider="file", params={"paths": [str(data)]}))
    result = resolve_and_capture("run_1", config)
    resource = result.snapshot.resources[str(data)]
    assert resource.version is not None
    assert resource.metadata["provider"] == "file"
    assert result.diagnostics == ()


def test_a_file_provider_keys_its_scope_the_way_it_keys_its_capture(tmp_path: Path) -> None:
    """A derived scope that disagrees with capture would fail closed on every
    file, so the two must key a path identically."""
    (tmp_path / "sub").mkdir()
    names = ["a.txt", "sub/b.txt", "c.txt"]
    for name in names:
        (tmp_path / name).write_text(name)
    paths = [
        str(tmp_path / f"./{name}") if "/" not in name else str(tmp_path / name) for name in names
    ]
    params = {"paths": paths}
    scope = FileProvider.scope_of(params)
    captured = FileProvider(params["paths"]).capture()
    assert scope == frozenset(captured)
    assert scope == frozenset(str(tmp_path / name) for name in names)


def test_a_relative_path_is_normalized_so_a_dot_prefix_cannot_split_a_key() -> None:
    """``./a.txt`` and ``a.txt`` are one resource, or two captures disagree."""
    assert FileProvider.scope_of({"paths": ["./a.txt"]}) == frozenset({"a.txt"})


def test_a_configured_file_provider_flags_a_changed_file_at_resume(
    store: SQLiteStorage, tmp_path: Path
) -> None:
    data = tmp_path / "data.csv"
    data.write_text("alpha")
    seed(store, dependency=str(data), version="v1")
    configure(store, ProviderSpec(provider="file", params={"paths": [str(data)]}))
    data.write_text("beta")

    decision = RecoveryEngine(store).assess("run_1")
    entry = dependency_entry(decision, str(data))
    assert entry.status in _BLOCKED  # type: ignore[attr-defined]
    assert "provider: file" in entry.detail  # type: ignore[attr-defined]


def test_a_configured_git_provider_reads_head(store: SQLiteStorage, tmp_path: Path) -> None:
    _init_repo(tmp_path)
    config = configure(store, ProviderSpec(provider="git", params={"path": str(tmp_path)}))
    result = resolve_and_capture("run_1", config)
    resource = result.snapshot.resources[f"git:{tmp_path}"]
    assert resource.version != UNKNOWN_VERSION
    assert resource.metadata["provider"] == "git"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


# --- registration and unavailable providers --------------------------------- #


def test_a_registered_callable_provider_is_applied_at_resume(store: SQLiteStorage) -> None:
    """A callable probe reaches configuration by name, never by serialization."""
    seed(store)
    registry = ProviderRegistry()
    registry.register("probe", CallableProvider({"dataset": lambda: "v3"}))
    configure(store, ProviderSpec(provider="probe", resources={"dataset"}))

    decision = RecoveryEngine(store, providers=registry).assess("run_1")
    assert decision.mode.value == "resume"
    assert decision.provider_diagnostics == ()


def test_an_unregistered_name_is_reported_unavailable_and_fails_closed(
    store: SQLiteStorage,
) -> None:
    seed(store)
    configure(store, ProviderSpec(provider="dataset-service", resources={"dataset"}))

    decision = RecoveryEngine(store).assess("run_1")
    (diagnostic,) = decision.provider_diagnostics
    assert diagnostic.status is ProviderStatus.UNAVAILABLE
    assert diagnostic.resources == frozenset({"dataset"})
    entry = dependency_entry(decision, "dataset")
    assert entry.status in _BLOCKED  # type: ignore[attr-defined]
    assert "dataset-service" in entry.detail  # type: ignore[attr-defined]


def test_a_registry_refuses_a_provider_that_is_not_one() -> None:
    registry = ProviderRegistry()
    with pytest.raises(TypeError, match="EnvironmentProvider"):
        registry.register("nope", object())  # type: ignore[arg-type]


# --- fail-closed paths ------------------------------------------------------ #


def test_a_disabled_provider_reports_its_resources_unknown(store: SQLiteStorage) -> None:
    seed(store)
    configure(
        store,
        ProviderSpec(provider="static", params={"resources": {"dataset": "v3"}}, enabled=False),
    )
    decision = RecoveryEngine(store).assess("run_1")
    (diagnostic,) = decision.provider_diagnostics
    assert diagnostic.status is ProviderStatus.DISABLED
    assert dependency_entry(decision, "dataset").status in _BLOCKED  # type: ignore[attr-defined]


def test_a_malformed_provider_reports_its_resources_unknown() -> None:
    config = ProviderConfig(specs=[ProviderSpec(provider="file", resources={"dataset"})])
    result = resolve_and_capture("run_1", config)
    (diagnostic,) = result.diagnostics
    assert diagnostic.status is ProviderStatus.MALFORMED
    assert "rejected its parameters" in diagnostic.detail
    assert result.snapshot.resources["dataset"].version == UNKNOWN_VERSION
    assert result.fail_closed


def test_a_raising_provider_reports_its_resources_unknown() -> None:
    class Exploding(EnvironmentProvider):
        name = "exploding"

        def capture(self) -> Mapping[str, Any]:
            raise RuntimeError("service down")

    registry = ProviderRegistry()
    registry.register("flaky", Exploding())
    config = ProviderConfig(specs=[ProviderSpec(provider="flaky", resources={"dataset"})])
    result = resolve_and_capture("run_1", config, registry=registry)
    (diagnostic,) = result.diagnostics
    assert diagnostic.status is ProviderStatus.FAILED
    assert "service down" in diagnostic.detail
    assert result.snapshot.resources["dataset"].version == UNKNOWN_VERSION


def test_a_probe_that_raises_reports_unknown_without_failing_the_capture() -> None:
    """A CallableProvider turns a raising probe into UNKNOWN itself; that is a
    finding on the resource, not a provider failure."""

    def boom() -> str:
        raise RuntimeError("service down")

    registry = ProviderRegistry()
    registry.register("flaky", CallableProvider({"dataset": boom}))
    config = ProviderConfig(specs=[ProviderSpec(provider="flaky", resources={"dataset"})])
    result = resolve_and_capture("run_1", config, registry=registry)
    assert result.snapshot.resources["dataset"].version == UNKNOWN_VERSION
    assert not result.fail_closed


def test_conflicting_resource_keys_fail_closed_for_both() -> None:
    config = ProviderConfig(
        specs=[
            ProviderSpec(provider="static", params={"resources": {"dataset": "v3"}}),
            ProviderSpec(provider="value", params={"dataset": "other"}),
        ]
    )
    assert sorted(config.conflicts()) == ["dataset"]
    result = resolve_and_capture("run_1", config)
    statuses = {d.provider: d.status for d in result.diagnostics}
    assert statuses == {"static": ProviderStatus.CONFLICT, "value": ProviderStatus.CONFLICT}
    assert result.snapshot.resources["dataset"].version == UNKNOWN_VERSION


def test_a_provider_reporting_outside_its_scope_is_marked_unknown() -> None:
    registry = ProviderRegistry()
    registry.register(
        "chatty",
        CallableProvider({"dataset": lambda: "v3", "surprise": lambda: "x"}),
    )
    config = ProviderConfig(specs=[ProviderSpec(provider="chatty", resources={"dataset"})])
    result = resolve_and_capture("run_1", config, registry=registry)
    assert result.snapshot.resources["surprise"].version == UNKNOWN_VERSION
    assert any(d.status is ProviderStatus.UNDECLARED for d in result.diagnostics)
    # A boundary warning is not a gap: the surplus was never trusted.
    assert not result.fail_closed


def test_a_declared_resource_the_provider_omits_is_reported_unknown() -> None:
    registry = ProviderRegistry()
    registry.register("quiet", CallableProvider({"dataset": lambda: "v3"}))
    config = ProviderConfig(
        specs=[ProviderSpec(provider="quiet", resources={"dataset", "catalog"})]
    )
    result = resolve_and_capture("run_1", config, registry=registry)
    assert result.snapshot.resources["catalog"].version == UNKNOWN_VERSION
    assert result.snapshot.resources["dataset"].version == "v3"


# --- determinism and persistence ------------------------------------------- #


def test_the_same_configuration_produces_identical_snapshots() -> None:
    config = ProviderConfig(
        specs=[
            ProviderSpec(provider="static", params={"resources": {"zeta": "1", "alpha": "2"}}),
            ProviderSpec(provider="value", params={"middle": "x"}),
        ]
    )
    first = resolve_and_capture("run_1", config)
    second = resolve_and_capture("run_1", config)
    # env_id and captured_at differ per snapshot by design; the sealed content
    # and the evidence must not.
    assert first.snapshot.integrity_hash == second.snapshot.integrity_hash
    assert first.snapshot.resources == second.snapshot.resources
    assert [d.detail for d in first.diagnostics] == [d.detail for d in second.diagnostics]


def test_a_configuration_survives_compaction(store: SQLiteStorage) -> None:
    """Compaction archives the prefix the configuration event lives in; resume
    reads the archived prefix too, so the configured provider still applies."""
    seed(store)
    configure(store, ProviderSpec(provider="static", params={"resources": {"dataset": "v3"}}))
    store.compact_run("run_1")
    archived = [e.type for e in store.read_archived_events("run_1")]
    assert EventType.ENVIRONMENT_PROVIDERS_CONFIGURED in archived

    decision = RecoveryEngine(store).assess("run_1")
    assert decision.mode.value == "resume"
    assert decision.provider_diagnostics == ()


def test_a_later_configuration_replaces_an_earlier_one(store: SQLiteStorage) -> None:
    configure(store, ProviderSpec(provider="nope", resources={"dataset"}))
    configure(store, ProviderSpec(provider="static", params={"resources": {"dataset": "v3"}}))
    seed(store)
    decision = RecoveryEngine(store).assess("run_1")
    assert decision.mode.value == "resume"


def test_an_unconfigured_run_behaves_exactly_as_before(store: SQLiteStorage) -> None:
    seed(store)
    decision = RecoveryEngine(store).assess("run_1")
    assert decision.provider_diagnostics == ()
    # No current environment was supplied and none was discovered, so nothing
    # was captured: the diff is empty and the run degrades on its own terms.
    assert not decision.validation.environment_diff.deltas


def test_an_explicit_environment_wins_over_configuration(store: SQLiteStorage) -> None:
    seed(store)
    configure(store, ProviderSpec(provider="nope", resources={"dataset"}))
    decision = RecoveryEngine(store).assess(
        "run_1", current_environment=capture("run_1", providers=[])
    )
    assert decision.provider_diagnostics == ()


# --- provenance in evidence and contracts ----------------------------------- #


def test_validation_evidence_names_the_provider_that_supplied_it(store: SQLiteStorage) -> None:
    seed(store)
    configure(store, ProviderSpec(provider="static", params={"resources": {"dataset": "v3"}}))
    decision = RecoveryEngine(store).assess("run_1")
    assert "provider: static" in dependency_entry(decision, "dataset").detail  # type: ignore[attr-defined]
    assert decision.contract.evidence
    assert any("provider: static" in line for line in decision.contract.evidence)


# --- the CLI ---------------------------------------------------------------- #


@pytest.fixture
def db(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "demo.db")
    with SQLiteStorage(path) as s:
        s.create_run(Run(run_id="run_1", goal="g"))
        s.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        yield path


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_add_and_list_round_trip(db: str, tmp_path: Path) -> None:
    data = tmp_path / "d.txt"
    data.write_text("one")
    code, out, errv = run_cli(
        "--db",
        db,
        "providers",
        "add",
        "run_1",
        "--provider",
        "file",
        "--resource",
        str(data),
    )
    assert code == 0, (out, errv)
    code, out, _ = run_cli("--db", db, "providers", "list", "run_1")
    assert code == 0
    assert "file [enabled]" in out


def test_cli_add_requires_resources_for_a_registered_name(db: str) -> None:
    code, _, err = run_cli("--db", db, "providers", "add", "run_1", "--provider", "dataset-service")
    assert code != 0
    assert "--resource is required" in err


def test_cli_add_refuses_a_secret_parameter(db: str) -> None:
    code, _, err = run_cli(
        "--db",
        db,
        "providers",
        "add",
        "run_1",
        "--provider",
        "static",
        "--resource",
        "dataset",
        "--param",
        "password=[REDACTED]",
    )
    assert code != 0
    assert "secret" in err


def test_cli_check_reports_failing_providers_in_json(db: str) -> None:
    run_cli(
        "--db",
        db,
        "providers",
        "add",
        "run_1",
        "--provider",
        "dataset-service",
        "--resource",
        "dataset",
    )
    code, out, _ = run_cli("--db", db, "--json", "providers", "check", "run_1")
    assert code == 0
    payload = json.loads(out)  # --json emits parseable JSON and nothing else
    assert payload["diagnostics"][0]["status"] == "unavailable"
    assert payload["fail_closed"] is True


def test_cli_list_json_is_parseable(db: str) -> None:
    run_cli(
        "--db",
        db,
        "providers",
        "add",
        "run_1",
        "--provider",
        "dataset-service",
        "--resource",
        "dataset",
    )
    code, out, _ = run_cli("--db", db, "--json", "providers", "list", "run_1")
    assert code == 0
    payload = json.loads(out)
    assert payload["providers"][0]["provider"] == "dataset-service"
    assert payload["conflicts"] == {}


def test_cli_remove_records_an_empty_set(db: str) -> None:
    run_cli(
        "--db",
        db,
        "providers",
        "add",
        "run_1",
        "--provider",
        "dataset-service",
        "--resource",
        "dataset",
    )
    code, out, _ = run_cli("--db", db, "providers", "remove", "run_1", "--all")
    assert code == 0, out
    with SQLiteStorage(db) as s:
        config = config_from_events(s.read_all_events("run_1"))
    assert config is not None
    assert config.specs == ()

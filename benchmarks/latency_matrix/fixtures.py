"""Deterministic fixtures for the recovery latency matrix (issue #766).

Everything here is generated: no network, no clock, no randomness. Given a file
count and a decision count the fixture is byte-identical on every machine, so a
measured regression points at code and not at the fixture.

The fixture mirrors ``scenario_large_state_recovery_latency`` in
``src/continuum/benchmark/phase6/scenarios.py`` (declare N dependencies, record
evidence and a finding for each, checkpoint, then bump one version so the
assessment has real drift to reason about) with two additions the plain scenario
does not have:

- A synthetic source tree of ``files`` modules, so the matrix exercises the
  source dependency graph the way a real harness would: built once outside the
  timed section and handed to ``assess_scoped`` as ``source_graph``. The graph
  build is the only place the file count costs anything (the doc's guidance is
  to cache it), so it is reported per point rather than buried in the timing.
- A repair ``scope`` of two resources, which is the path ``files_using`` walks
  and the localized-recovery usage the latency note recommends measuring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from continuum.analysis import DependencyGraph
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import EnvironmentSnapshot, EnvResource, Run
from continuum.storage import SQLiteStorage

RUN_ID = "matrix_run"

# Two of the declared resources form the repair scope. A scope is what makes the
# source graph participate in the decision at all (engine.py only walks
# ``files_using`` when both ``source_graph`` and ``scope`` are set), so a matrix
# without one would measure a path that never touches the file dimension.
SCOPE: tuple[str, ...] = ("dep0", "dep1")


@dataclass(frozen=True, slots=True)
class Fixture:
    """One matrix point's immutable inputs, built once and assessed repeatedly."""

    files: int
    decisions: int
    storage: SQLiteStorage
    graph: DependencyGraph
    # The only place the file dimension costs time is building the graph, which
    # is why it is built once here and reused for every timed sample. Reported
    # per point rather than folded into the assessment timing.
    graph_build_ms: float
    scope: tuple[str, ...]
    current_environment: EnvironmentSnapshot


def build_repo(root: Path, n_files: int, n_deps: int) -> None:
    """Write ``n_files`` deterministic modules importing declared dependencies.

    File ``i`` imports ``dep{i % n_deps}``, so the graph's package map is evenly
    populated and ``files_using`` returns a non-empty set for every scoped
    resource at every point in the matrix.
    """
    for i in range(n_files):
        (root / f"module_{i:04d}.py").write_text(
            f"import os\nimport sys\nimport dep{i % n_deps}\n",
            encoding="utf-8",
        )


def build_storage(n_decisions: int) -> SQLiteStorage:
    """Build an in-memory run with ``n_decisions`` declared dependencies.

    Each dependency carries its own evidence and finding, the triple that drives
    validation, provenance and planner work during assessment.
    """
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id=RUN_ID, goal="latency matrix"))
    storage.append_event(RUN_ID, EventType.RUN_STARTED, {"goal": "latency matrix", "total": 10})
    for i in range(n_decisions):
        storage.append_event(
            RUN_ID, EventType.DEPENDENCY_DECLARED, {"resource": f"dep{i}", "version": "v1"}
        )
        storage.append_event(
            RUN_ID,
            EventType.EVIDENCE_ADDED,
            {"evidence_id": f"e{i}", "summary": "x", "source": f"dep{i}"},
        )
        storage.append_event(
            RUN_ID,
            EventType.FINDING_ADDED,
            {"finding_id": f"f{i}", "claim": "x", "evidence": [f"e{i}"]},
        )
    base = {f"dep{i}": EnvResource(name=f"dep{i}", version="v1") for i in range(n_decisions)}
    CheckpointManager(storage).checkpoint(
        RUN_ID, environment=capture(RUN_ID, StaticProvider(resources=base))
    )
    return storage


def current_snapshot(n_decisions: int) -> EnvironmentSnapshot:
    """The post-drift environment: ``dep0`` moved, so assessment has work to do."""
    base = {f"dep{i}": EnvResource(name=f"dep{i}", version="v1") for i in range(n_decisions)}
    base["dep0"] = EnvResource(name="dep0", version="v2")
    return capture(RUN_ID, StaticProvider(resources=base))


def make_fixture(repo_root: Path, files: int, decisions: int) -> Fixture:
    """Build one fixture: source tree plus graph plus populated storage."""
    if files < 1 or decisions < 2:
        raise ValueError(
            f"matrix points must be >= 1 file and >= 2 decisions, got {files}/{decisions}"
        )
    repo_root.mkdir(parents=True, exist_ok=True)
    build_repo(repo_root, files, decisions)
    requirements = [f"dep{i}" for i in range(decisions)]
    start = perf_counter()
    graph = DependencyGraph(repo_root, requirements=requirements)
    graph_build_ms = (perf_counter() - start) * 1000.0
    return Fixture(
        files=files,
        decisions=decisions,
        storage=build_storage(decisions),
        graph=graph,
        graph_build_ms=graph_build_ms,
        scope=SCOPE,
        current_environment=current_snapshot(decisions),
    )


def fixture_summary(fixture: Fixture) -> dict[str, Any]:
    """Deterministic, clock-free facts about a fixture, for the report metadata.

    Deliberately excludes timings: this is the part of the record that proves the
    fixture did not change between the baseline run and this one.
    """
    return {
        "files": fixture.files,
        "decisions": fixture.decisions,
        "events": len(fixture.storage.read_events(RUN_ID)),
        "graph_files_scanned": len(fixture.graph._file_imports),
        "scope": list(fixture.scope),
    }

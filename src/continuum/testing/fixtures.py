"""A deterministic environment fixture factory (issue #163).

Benchmarks and scenarios need reproducible environments with injected
failures. :func:`environment_fixture` spins up, in a temp directory with no
network access:

* a seeded run declaring its dependencies, each with evidence and a finding,
  and an initial checkpoint;
* a matching file layout (one module per dependency plus a ``pyproject.toml``),
  so the source-level dependency graph scans it like a real repo;
* a :class:`FixtureAdapter` whose named action types can be made to fail, which
  records the attempt in the ActionLedger before failing, exactly like a real
  interrupted side effect.

Two fixtures built with the same arguments produce byte-identical file layout
and identical validation verdicts.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from continuum.adapters.generic import GenericAgentAdapter
from continuum.analysis.depends import DependencyGraph as SourceDependencyGraph
from continuum.checkpoint import CheckpointManager
from continuum.environment import StaticProvider, capture
from continuum.events import EventType
from continuum.models import EnvironmentSnapshot, EnvResource, Run
from continuum.recovery import RecoveryEngine
from continuum.storage import SQLiteStorage

__all__ = [
    "EnvironmentFixture",
    "FixtureAdapter",
    "InjectedFailures",
    "environment_fixture",
]


@dataclass(frozen=True, slots=True)
class InjectedFailures:
    """Action types that fail when attempted through the fixture adapter."""

    action_types: frozenset[str] = field(default_factory=frozenset)


class FixtureAdapter(GenericAgentAdapter):
    """A generic adapter that fails configured action types on attempt.

    The failure happens inside the wrapped callable, after the ledger claim, so
    the recorded side effect is uncertain rather than absent. That is the shape
    a real network blip takes.
    """

    def __init__(self, storage: SQLiteStorage, *, failures: InjectedFailures | None = None) -> None:
        super().__init__(storage)
        self.failures = failures or InjectedFailures()

    def intercept_action(self, run_id, action_type, action_fn, arguments=None, **kwargs):  # type: ignore[no-untyped-def]
        if action_type in self.failures.action_types:

            def injected() -> None:
                raise RuntimeError(f"injected failure: {action_type}")

            return super().intercept_action(run_id, action_type, injected, arguments, **kwargs)
        return super().intercept_action(run_id, action_type, action_fn, arguments, **kwargs)


class EnvironmentFixture:
    """A deterministic agent environment rooted at a temp directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str = "run_1",
        goal: str = "fixture task",
        dependencies: tuple[str, ...] = ("dataset",),
        base_version: str = "v1",
        failures: InjectedFailures | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.dependencies = tuple(dependencies)
        self.base_version = base_version

        self._write_repo_layout()

        self.storage = SQLiteStorage(str(self.root / "continuum.db"))
        self.adapter = FixtureAdapter(self.storage, failures=failures)
        self.engine = RecoveryEngine(self.storage)
        self._seed_run(goal)

    # -- construction helpers ------------------------------------------------ #

    def _write_repo_layout(self) -> None:
        deps = ", ".join(f'"{d}"' for d in self.dependencies)
        (self.root / "pyproject.toml").write_text(
            f'[project]\nname = "fixture"\ndependencies = [{deps}]\n', encoding="utf-8"
        )
        for dep in self.dependencies:
            (self.root / f"{dep}_impl.py").write_text(f"import {dep}\n", encoding="utf-8")

    def _seed_run(self, goal: str) -> None:
        storage = self.storage
        storage.create_run(Run(run_id=self.run_id, goal=goal))
        storage.append_event(self.run_id, EventType.RUN_STARTED, {"goal": goal, "total": 1})
        for dep in self.dependencies:
            storage.append_event(
                self.run_id,
                EventType.DEPENDENCY_DECLARED,
                {"resource": dep, "version": self.base_version},
            )
            storage.append_event(
                self.run_id,
                EventType.EVIDENCE_ADDED,
                {"evidence_id": f"ev_{dep}", "summary": f"observed {dep}", "source": dep},
            )
            storage.append_event(
                self.run_id,
                EventType.FINDING_ADDED,
                {
                    "finding_id": f"f_{dep}",
                    "claim": f"{dep} holds",
                    "evidence": [f"ev_{dep}"],
                },
            )
        CheckpointManager(storage).checkpoint(self.run_id, environment=self.capture())

    # -- surface ------------------------------------------------------------- #

    def capture(self, **overrides: str) -> EnvironmentSnapshot:
        """Snapshot every dependency at baseline, with per-dependency overrides."""
        versions = dict.fromkeys(self.dependencies, self.base_version)
        versions.update(overrides)
        return capture(
            self.run_id,
            StaticProvider(
                resources={
                    name: EnvResource(name=name, version=version)
                    for name, version in versions.items()
                }
            ),
        )

    def source_graph(self) -> SourceDependencyGraph:
        """The source-level dependency graph over the fixture's file layout."""
        return SourceDependencyGraph(self.root)

    def close(self) -> None:
        self.storage.close()


@contextmanager
def environment_fixture(**kwargs: Any) -> Iterator[EnvironmentFixture]:
    """Build an :class:`EnvironmentFixture` in a fresh temp directory."""
    with TemporaryDirectory() as tmp:
        fixture = EnvironmentFixture(tmp, **kwargs)
        try:
            yield fixture
        finally:
            fixture.close()

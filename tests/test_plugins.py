"""The plugin Registry and the four capability seams.

The property under test: a plugin is any object registered under a name and
conforming to a seam ``Protocol``, and the built-in providers already conform
to their seams. ``GitProvider`` is the first *discoverable* environment
provider: it reads the world instead of trusting a declared version.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from continuum.environment import EnvironmentProvider, GitProvider, StaticProvider
from continuum.models import (
    Action,
    Component,
    ComponentValidationEntry,
    Goal,
    SemanticState,
    StateStatus,
)
from continuum.plugins import (
    ActionReconciler,
    Reconciliation,
    Registry,
    StateExtractor,
    ValidationRule,
)
from continuum.state.extractor import ExtractionContext

# --- Registry ---------------------------------------------------------------


def test_register_and_resolve_by_type() -> None:
    reg = Registry()
    reg.register("thing", "a-string")
    assert reg.get("thing", str) == "a-string"
    assert "thing" in reg
    assert len(reg) == 1


def test_duplicate_names_are_rejected() -> None:
    reg = Registry()
    reg.register("x", 1)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("x", 2)


def test_get_rejects_wrong_type_and_missing_name() -> None:
    reg = Registry()
    reg.register("x", "s")
    with pytest.raises(TypeError):
        reg.get("x", int)
    with pytest.raises(KeyError):
        reg.get("missing", str)


def test_get_optional_returns_none_when_absent() -> None:
    assert Registry().get_optional("x", str) is None


def test_registration_is_reversible() -> None:
    reg = Registry()
    handle = reg.register("x", 1)
    assert "x" in reg
    handle.unregister()
    assert "x" not in reg


def test_all_of_filters_by_type() -> None:
    reg = Registry()
    reg.register("a", StaticProvider())
    reg.register("b", "not-a-provider")
    found = reg.all_of(EnvironmentProvider)
    assert len(found) == 1
    assert isinstance(found[0], StaticProvider)


# --- seam conformance -------------------------------------------------------


class _DummyExtractor:
    name = "dummy"

    def extract(self, context: ExtractionContext) -> SemanticState:
        return SemanticState(run_id="r", goal=Goal(description="g"))


class _DummyReconciler:
    name = "dummy"

    def reconcile(self, action: Action) -> Reconciliation:
        return Reconciliation(occurred=True, external_id="ext-1")


class _DummyRule:
    name = "dummy"

    def evaluate(self, state, environment=None) -> list[ComponentValidationEntry]:
        return [ComponentValidationEntry(component=Component.GOAL, status=StateStatus.VALID)]


def test_state_extractor_seam_is_satisfied() -> None:
    assert isinstance(_DummyExtractor(), StateExtractor)
    ctx = ExtractionContext(run_id="r", trajectory=())
    state = _DummyExtractor().extract(ctx)
    assert isinstance(state, SemanticState)


def test_action_reconciler_seam_is_satisfied() -> None:
    assert isinstance(_DummyReconciler(), ActionReconciler)
    result = _DummyReconciler().reconcile(Action(run_id="r", action_type="x"))
    assert result.occurred is True
    assert result.external_id == "ext-1"


def test_validation_rule_seam_is_satisfied() -> None:
    assert isinstance(_DummyRule(), ValidationRule)
    out = _DummyRule().evaluate(SemanticState(run_id="r", goal=Goal(description="g")))
    assert out[0].component is Component.GOAL
    assert out[0].status is StateStatus.VALID


def test_builtin_providers_conform_to_their_seams() -> None:
    assert isinstance(StaticProvider(), EnvironmentProvider)
    assert isinstance(GitProvider(), EnvironmentProvider)


# --- GitProvider (a discoverable environment provider) ----------------------


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_git_provider_reads_head_in_a_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    resources = GitProvider(tmp_path).capture()
    key = f"git:{tmp_path}"
    assert key in resources
    assert resources[key].version != "__unknown__"
    assert resources[key].metadata["commit"]


def test_git_provider_reports_unknown_outside_a_repo(tmp_path: Path) -> None:
    resources = GitProvider(tmp_path).capture()
    key = f"git:{tmp_path}"
    assert resources[key].version == "__unknown__"
    assert "error" in resources[key].metadata

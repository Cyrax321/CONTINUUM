"""The Docker release gate must run before the image is published (#1567).

``docker-publish.yml`` used to push ``:latest`` to GHCR and *then* smoke-test
it, so every failing build on ``main`` still replaced the public image with a
broken one: the smoke test diagnosed a broken release instead of preventing
one. The gate now builds to the local daemon, runs the crash-recovery demo
against that local image, and only publishes once the demo passes. This pins
the ordering so a later edit cannot put the publish back in front of the gate.

PyYAML is not a first-class test dependency (it arrives with the ``mcp`` and
``langchain`` extras), so the suite skips rather than fails when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "docker-publish.yml"


def _steps() -> list[dict[str, object]]:
    yaml = pytest.importorskip("yaml")
    with WORKFLOW.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    steps: list[dict[str, object]] = document["jobs"]["publish"]["steps"]
    assert isinstance(steps, list)
    for step in steps:
        assert isinstance(step, dict)
    return steps


def _pushes(step: dict[str, object]) -> bool:
    """A build-push step that makes the image reachable from the registry."""
    with_block = step.get("with")
    return isinstance(with_block, dict) and bool(with_block.get("push"))


def _is_the_gate(step: dict[str, object]) -> bool:
    return "smoke" in str(step.get("name", "")).lower()


def test_the_smoke_gate_precedes_every_publish() -> None:
    """No image may reach the registry until the demo has passed."""
    steps = _steps()
    gate = next((index for index, step in enumerate(steps) if _is_the_gate(step)), None)
    assert gate is not None, "the smoke-test step is gone from docker-publish.yml"

    publishes = [index for index, step in enumerate(steps) if _pushes(step)]
    assert publishes, (
        "no step pushes the image, so the publish path moved and this guard must follow it"
    )
    first_publish = min(publishes)
    assert first_publish > gate, (
        f"step {first_publish} publishes before the smoke gate at step {gate}: "
        "a failing build can still replace the public :latest tag before the "
        "gate fires (issue #1567)"
    )


def test_the_pre_gate_build_does_not_publish() -> None:
    """The build feeding the gate stays local, or the gate gates nothing."""
    steps = _steps()
    gate = next((index for index, step in enumerate(steps) if _is_the_gate(step)), None)
    assert gate is not None, "the smoke-test step is gone from docker-publish.yml"

    pre_gate = [step for step in steps[:gate] if "build-push-action" in str(step)]
    for step in pre_gate:
        assert not _pushes(step), (
            "a build before the smoke gate pushes, so the unverified image is "
            "already public when the gate runs (issue #1567)"
        )

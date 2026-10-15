from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import AdapterAction, KubernetesAdapter
from continuum.models import Run
from continuum.storage import SQLiteStorage


def _storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_k8s", goal="run a Kubernetes job"))
    return storage


@pytest.mark.parametrize(
    ("kubectl", "kubernetes_importable", "expected"),
    [
        (None, False, False),
        (None, True, False),
        ("/usr/bin/kubectl", False, False),
        ("/usr/bin/kubectl", True, True),
    ],
)
def test_available_requires_kubectl_and_python_package(
    monkeypatch: pytest.MonkeyPatch,
    kubectl: str | None,
    kubernetes_importable: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr("continuum.adapters.kubernetes.shutil.which", lambda _: kubectl)
    package = ModuleType("kubernetes")
    monkeypatch.setitem(sys.modules, "kubernetes", package if kubernetes_importable else None)

    assert KubernetesAdapter.available() is expected


def test_init_stores_storage_and_namespace() -> None:
    storage = _storage()

    adapter = KubernetesAdapter(storage, namespace="jobs")

    assert adapter.storage is storage
    assert adapter.namespace == "jobs"


def test_run_job_builds_the_expected_action_and_returns_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage()
    adapter = KubernetesAdapter(storage, namespace="jobs")
    monkeypatch.setattr(KubernetesAdapter, "available", staticmethod(lambda: True))
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout="job output")

    def intercept_action(
        run_id: str,
        action_type: str,
        action_fn: Callable[[], str],
        *,
        arguments: dict[str, str],
        dep_scope: str | None,
    ) -> str:
        captured["run_id"] = run_id
        captured["action"] = AdapterAction(
            name=action_type,
            params=arguments,
            dep_scope=dep_scope,
        )
        return action_fn()

    monkeypatch.setattr("continuum.adapters.kubernetes.subprocess.run", fake_run)
    monkeypatch.setattr(adapter, "intercept_action", intercept_action)

    result = adapter.run_job(
        "run_k8s",
        "example/app:latest",
        "python worker.py",
        dep_scope="kubernetes",
    )

    assert result.status == "completed"
    assert result.output == "job output"
    assert captured["run_id"] == "run_k8s"
    assert captured["action"] == AdapterAction(
        name="k8s.job",
        params={"image": "example/app:latest", "command": "python worker.py"},
        dep_scope="kubernetes",
    )
    assert captured["args"] == [
        "kubectl",
        "run",
        "continuum-job",
        "-n",
        "jobs",
        "--image",
        "example/app:latest",
        "--",
        "sh",
        "-c",
        "python worker.py",
    ]
    assert captured["kwargs"] == {"capture_output": True, "text": True, "check": True}


def test_run_job_captures_subprocess_failure_in_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = KubernetesAdapter(_storage())
    monkeypatch.setattr(KubernetesAdapter, "available", staticmethod(lambda: True))

    def fail_run(*_: object, **__: object) -> None:
        raise subprocess.CalledProcessError(
            1,
            ["kubectl", "run"],
            stderr="image pull failed",
        )

    monkeypatch.setattr("continuum.adapters.kubernetes.subprocess.run", fail_run)

    result = adapter.run_job("run_k8s", "missing/image", "echo hi")

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.startswith("CalledProcessError:")
    assert len(ActionLedger(adapter.storage, "run_k8s").pending()) == 1

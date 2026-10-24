from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from continuum.actions.ledger import ActionLedger
from continuum.adapters import AdapterAction, KubernetesAdapter
from continuum.adapters.kubernetes import _pod_name
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
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        captured["args"] = list(args)
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
    # Every job gets its own pod name derived from the run and the arguments
    # (#1807): a shared "continuum-job" left the first job's completed pod
    # behind, and every job after the first was refused with AlreadyExists.
    pod_name = captured["args"][2]
    assert re.fullmatch(r"continuum-job-run-k8s-[0-9a-f]{12}", pod_name)
    assert captured["args"][:3] == ["kubectl", "run", pod_name]
    assert captured["args"][3:] == [
        "-n",
        "jobs",
        "--image",
        "example/app:latest",
        "--rm",
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


def _adapter_running(
    monkeypatch: pytest.MonkeyPatch, storage: SQLiteStorage
) -> tuple[KubernetesAdapter, list[list[str]]]:
    """An adapter whose only kubectl behavior visible to the test is its argv."""
    monkeypatch.setattr(KubernetesAdapter, "available", staticmethod(lambda: True))
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(stdout="job output")

    monkeypatch.setattr("continuum.adapters.kubernetes.subprocess.run", fake_run)
    return KubernetesAdapter(storage), calls


def test_run_job_name_is_unique_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two jobs in one namespace must not share a pod name: kubectl refuses the
    # second with AlreadyExists when the completed pod is still around (#1807).
    adapter, calls = _adapter_running(monkeypatch, _storage())

    adapter.run_job("run_k8s", "example/app:latest", "python worker.py")
    adapter.run_job("run_k8s", "example/app:latest", "python other.py")
    adapter.run_job("run_k8s", "example/app:other", "python worker.py")

    names = [argv[2] for argv in calls]
    assert len(set(names)) == len(names) == 3


def test_pod_name_is_stable_for_a_replayed_attempt() -> None:
    # The name is derived from the same inputs the action ledger keys its claim
    # on, so a retry of one attempt lands on that attempt's own pod name rather
    # than inventing a new one, and changing any input moves it.
    stable = ("run_k8s", "example/app:latest", "python worker.py")
    name = _pod_name(*stable)

    assert _pod_name(*stable) == name
    assert _pod_name("run_other", *stable[1:]) != name
    assert _pod_name(stable[0], "example/app:other", stable[2]) != name
    assert _pod_name(stable[0], stable[1], "python other.py") != name


def test_run_job_name_is_a_valid_dns_subdomain(monkeypatch: pytest.MonkeyPatch) -> None:
    # Run ids are free-form ("Run_123" is as legal as "run.2"); pod names are
    # not, and kubectl rejects anything outside RFC 1123.
    adapter, calls = _adapter_running(monkeypatch, _storage())

    adapter.run_job("Run_With_Underscores_2026", "example/app:latest", "echo hi")
    adapter.run_job("x" * 500, "example/app:latest", "echo hi")

    for argv in calls:
        name = argv[2]
        assert len(name) <= 253
        assert re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name), name


def test_run_job_removes_the_pod_when_the_command_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The container adapter cleans up with `docker run --rm`; the k8s path used
    # to leave one completed pod behind per job, and they pile up (#1807).
    adapter, calls = _adapter_running(monkeypatch, _storage())

    adapter.run_job("run_k8s", "example/app:latest", "python worker.py")

    assert "--rm" in calls[0]
    # --rm is a kubectl flag, so it must precede the command separator.
    assert calls[0].index("--rm") < calls[0].index("--")

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

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


def _adapter(
    monkeypatch: pytest.MonkeyPatch,
    storage: SQLiteStorage | None = None,
    *,
    namespace: str = "default",
) -> tuple[KubernetesAdapter, list[list[str]]]:
    """An available adapter whose every kubectl argv is recorded.

    ``intercept_action`` is bypassed, as the pinned-argv test does, so the
    ledger's own deduplication cannot swallow a second call the regression
    tests need to see reach kubectl.
    """
    adapter = KubernetesAdapter(storage or _storage(), namespace=namespace)
    monkeypatch.setattr(KubernetesAdapter, "available", staticmethod(lambda: True))
    argvs: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> SimpleNamespace:
        argvs.append(args)
        return SimpleNamespace(stdout="job output")

    def intercept_action(
        run_id: str,
        action_type: str,
        action_fn: Callable[[], str],
        **_: object,
    ) -> str:
        return action_fn()

    monkeypatch.setattr("continuum.adapters.kubernetes.subprocess.run", fake_run)
    monkeypatch.setattr(adapter, "intercept_action", intercept_action)
    return adapter, argvs


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
        "continuum-job-0786df27ca76",
        "-n",
        "jobs",
        "--attach",
        "--rm",
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


def test_run_job_gives_each_job_in_a_namespace_a_distinct_pod_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every job after the first no longer loses the race for one name (#1807).

    With a hardcoded name the pod survived the first call, and the API server
    refused the next one with ``AlreadyExists``, so a namespace could run
    exactly one ``run_job``, ever, and two concurrent runs collided even on
    the first. Distinct commands and distinct images within one run now
    reach kubectl under distinct names.
    """
    adapter, argvs = _adapter(monkeypatch, namespace="jobs")

    adapter.run_job("run_k8s", "example/app:latest", "python worker.py")
    adapter.run_job("run_k8s", "example/app:latest", "python other.py")
    adapter.run_job("run_k8s", "example/app:other", "python worker.py")

    pod_names = [argv[2] for argv in argvs]
    assert len(pod_names) == 3
    assert len(set(pod_names)) == 3
    for name in pod_names:
        assert name != "continuum-job"
        assert _pod_name_matches_dns1123(name)


def test_run_job_reuses_one_pod_name_for_a_replay_of_the_same_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay of the same logical job lands on the same name (#1807).

    The name is derived from the run id and the action's idempotency basis,
    so a recovery replay does not invent a second pod for one job.
    """
    adapter, argvs = _adapter(monkeypatch)

    adapter.run_job("run_k8s", "example/app:latest", "python worker.py")
    adapter.run_job("run_k8s", "example/app:latest", "python worker.py")
    adapter.run_job("run_other", "example/app:latest", "python worker.py")

    pod_names = [argv[2] for argv in argvs]
    assert (
        pod_names[0]
        == pod_names[1]
        == _pod_name("run_k8s", "example/app:latest", "python worker.py")
    )
    assert pod_names[2] != pod_names[0]


def test_pod_name_stays_a_valid_label_when_the_run_id_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run ids that kubernetes would reject must not leak into the name.

    ``run_k8s`` carries an underscore and this fixture's other ids uppercase
    letters; interpolation would produce an invalid DNS-1123 label, so the
    name is built from hex digest characters instead.
    """
    adapter, argvs = _adapter(monkeypatch)

    adapter.run_job("Run_K8s_2026", "example/app:latest", "python worker.py")

    assert _pod_name_matches_dns1123(argvs[0][2])
    assert _pod_name("Run_K8s_2026", "example/app:latest", "python worker.py").startswith(
        "continuum-job-"
    )


def _pod_name_matches_dns1123(name: str) -> bool:
    return re.fullmatch(r"continuum-job-[0-9a-f]{12}", name) is not None


def test_run_job_attaches_so_rm_is_legal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--rm`` only parses on an attached run, so ``--attach`` goes with it (#1849).

    ``kubectl run`` rejects ``--rm`` outright when it is not attaching (``--rm
    should only be used for attached containers``), failing before it creates
    any pod. A job argv that names ``--rm`` without ``--attach`` therefore
    cannot have ever run on a live cluster.
    """
    adapter, argvs = _adapter(monkeypatch)

    adapter.run_job("run_k8s", "busybox", "echo hello")

    argv = argvs[0]
    assert "--rm" in argv
    assert argv.index("--attach") < argv.index("--rm")

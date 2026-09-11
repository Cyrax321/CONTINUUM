"""Kubernetes environment adapter.

Optional dependency: the ``kubernetes`` client must be installed and a cluster
configured. The adapter imports it lazily; smoke tests skip when absent. It
reuses the container image-run model from issue #116. See issue #159.
"""

from __future__ import annotations

import shutil
import subprocess

from continuum.adapters import run_action
from continuum.adapters.actions import AdapterAction, AdapterResult
from continuum.adapters.generic import GenericAgentAdapter
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.base import Storage


class KubernetesAdapter(GenericAgentAdapter):
    """Runs a one-shot job on a Kubernetes cluster, recorded as an action."""

    @staticmethod
    def available() -> bool:
        if shutil.which("kubectl") is None:
            return False
        try:
            import kubernetes  # noqa: F401

            return True
        except ImportError:
            return False

    def __init__(
        self,
        storage: Storage,
        *,
        namespace: str = "default",
        engine: RecoveryEngine | None = None,
    ) -> None:
        super().__init__(storage, engine=engine)
        self.namespace = namespace

    def run_job(
        self, run_id: str, image: str, command: str, *, dep_scope: str | None = None
    ) -> AdapterResult:
        if not self.available():
            raise RuntimeError("kubectl or the kubernetes client is not available")

        def _run() -> str:
            completed = subprocess.run(
                [
                    "kubectl",
                    "run",
                    "continuum-job",
                    "-n",
                    self.namespace,
                    "--image",
                    image,
                    "--",
                    "sh",
                    "-c",
                    command,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return completed.stdout

        return run_action(
            self,
            run_id,
            AdapterAction(
                name="k8s.job", params={"image": image, "command": command}, dep_scope=dep_scope
            ),
            _run,
        )

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
from continuum.security.hashing import stable_hash
from continuum.storage.base import Storage

__all__ = [
    "KubernetesAdapter",
]


def _pod_name(run_id: str, image: str, command: str) -> str:
    """A pod name unique to this (run, image, command) triple.

    A fixed name let a namespace run exactly one ``run_job``, ever: the pod
    stayed behind after the first call, and the API server then refused the
    same name with ``AlreadyExists`` (issue #1807). Hashing the triple instead
    of interpolating it gives distinct calls distinct names while a replay of
    the same logical job, the same run, image and command, lands on the same
    name again. Only hex characters survive into the name, so a run id with
    characters kubernetes rejects still yields a valid DNS-1123 label.
    """
    digest = stable_hash({"run_id": run_id, "image": image, "command": command})
    return f"continuum-job-{digest[:12]}"


class KubernetesAdapter(GenericAgentAdapter):
    """Runs a one-shot job on a Kubernetes cluster, recorded as an action."""

    @staticmethod
    def available() -> bool:
        """Whether both halves of the kubernetes toolchain are present.

        Requires the ``kubectl`` CLI on PATH *and* the ``kubernetes`` Python
        package importable; either missing means unavailable. This does not
        check that a cluster is reachable: that surfaces at execution time.
        Smoke tests skip on ``False`` instead of failing.
        """
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
        """Run ``sh -c command`` as a one-shot pod from ``image``.

        Executes ``kubectl run <pod-name> -n <namespace> --rm --image <image>``
        in the adapter's namespace and returns the pod's stdout as
        ``AdapterResult.output``. The pod name is derived from the run id and
        the action's idempotency basis (its image and command), so two
        different jobs in one namespace get distinct names and stop
        colliding, while a replay of the same job reuses its own name.
        ``--rm`` deletes the pod when it completes, the same cleanup the
        container adapter gets from ``docker run --rm``; without it every
        completed pod stays behind and blocks its own name from ever being
        used again (issue #1807). A non-zero exit or a kubectl failure is
        captured into the result (status ``failed`` with ``error`` set)
        rather than raised, because the attempt is already recorded in the
        ledger. Raises ``RuntimeError`` up front when kubectl or the
        kubernetes client is missing. ``dep_scope`` names the dependency the
        action should be scoped to for recovery.
        """
        if not self.available():
            raise RuntimeError("kubectl or the kubernetes client is not available")

        def _run() -> str:
            completed = subprocess.run(
                [
                    "kubectl",
                    "run",
                    _pod_name(run_id, image, command),
                    "-n",
                    self.namespace,
                    "--rm",
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

"""Kubernetes environment adapter.

Optional dependency: the ``kubernetes`` client must be installed and a cluster
configured. The adapter imports it lazily; smoke tests skip when absent. It
reuses the container image-run model from issue #116. See issue #159.
"""

from __future__ import annotations

import re
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

# Kubernetes resource names are DNS subdomains: lowercase alphanumerics with
# '-' and '.', bounded by 253 characters (RFC 1123).
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9.-]+")
_NAME_PREFIX = "continuum-job-"
_NAME_HASH_LENGTH = 12
# 253 for the whole name, less the prefix and the '-<hash>' suffix.
_MAX_NAME_STEM = 253 - len(_NAME_PREFIX) - _NAME_HASH_LENGTH - 1


def _pod_name(run_id: str, image: str, command: str) -> str:
    """Derive the pod name for one ``run_job`` (issue #1807).

    The name is deterministic in the run and the action's arguments -- the same
    inputs the action ledger keys its claim on -- so a retry of one attempt
    re-uses that attempt's own name, while two jobs in one namespace get two
    names instead of racing for a shared ``continuum-job``.
    """
    stem = _INVALID_NAME_CHARS.sub("-", run_id.lower()).strip("-.")[:_MAX_NAME_STEM]
    digest = stable_hash({"run_id": run_id, "image": image, "command": command})[:_NAME_HASH_LENGTH]
    return f"{_NAME_PREFIX}{stem}-{digest}" if stem else f"{_NAME_PREFIX}{digest}"


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

        Executes ``kubectl run continuum-job-<run>-<hash> -n <namespace>
        --image <image> --rm`` in the adapter's namespace and returns the pod's
        stdout as ``AdapterResult.output``. The pod name is derived from the
        run id and the action's arguments, so every job gets its own name
        instead of every job after the first colliding with the lingering
        predecessor; a retry of the same attempt lands on the same name. ``--rm``
        removes the pod when it finishes, so the name is free for the next run
        and nothing accumulates in the namespace. A non-zero exit or a kubectl
        failure is captured into the result (status ``failed`` with ``error``
        set) rather than raised, because the attempt is already recorded in the
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
                    "--image",
                    image,
                    "--rm",
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

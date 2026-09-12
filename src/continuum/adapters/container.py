"""Container environment adapter (docker).

Requires the ``docker`` CLI on PATH. The adapter is importable without docker
installed; execution raises a clear error, and smoke tests skip when docker is
unavailable. This is the container leg of issue #116. It reuses the same
uniform action facade as the other environment adapters.
"""

from __future__ import annotations

import shutil
import subprocess

from continuum.adapters import run_action
from continuum.adapters.actions import AdapterAction, AdapterResult
from continuum.adapters.generic import GenericAgentAdapter
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.base import Storage


class ContainerAdapter(GenericAgentAdapter):
    """Runs commands inside a docker container, recorded as an action."""

    @staticmethod
    def available() -> bool:
        """Return whether the docker CLI is available on PATH."""
        return shutil.which("docker") is not None

    def __init__(
        self,
        storage: Storage,
        image: str,
        *,
        engine: RecoveryEngine | None = None,
    ) -> None:
        super().__init__(storage, engine=engine)
        self.image = image

    def run_in_container(
        self, run_id: str, command: str, *, dep_scope: str | None = None
    ) -> AdapterResult:
        """Run a shell command in the configured image and return its recorded result."""
        if not self.available():
            raise RuntimeError("docker is not available on PATH")

        def _run() -> str:
            completed = subprocess.run(
                ["docker", "run", "--rm", self.image, "sh", "-c", command],
                capture_output=True,
                text=True,
                check=True,
            )
            return completed.stdout

        return run_action(
            self,
            run_id,
            AdapterAction(
                name="container",
                params={"command": command, "image": self.image},
                dep_scope=dep_scope,
            ),
            _run,
        )

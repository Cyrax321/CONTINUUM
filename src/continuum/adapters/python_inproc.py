"""In-process Python environment adapter.

Runs Python snippets in a sandbox directory and records each run as an
idempotent action, so recovery parity with the shell adapter holds. Safe in CI:
it uses the same interpreter that is running CONTINUUM, so no external service
is required. See issue #116.
"""

from __future__ import annotations

import subprocess
import sys

from continuum.adapters import run_action
from continuum.adapters.actions import AdapterAction, AdapterResult
from continuum.adapters.generic import GenericAgentAdapter
from continuum.recovery.engine import RecoveryEngine
from continuum.storage.base import Storage

__all__ = [
    "PythonInProcAdapter",
]


class PythonInProcAdapter(GenericAgentAdapter):
    """Executes Python code in a local work directory, recorded as an action."""

    def __init__(
        self,
        storage: Storage,
        workdir: str,
        *,
        engine: RecoveryEngine | None = None,
    ) -> None:
        super().__init__(storage, engine=engine)
        from pathlib import Path

        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run_python(self, run_id: str, code: str, *, dep_scope: str | None = None) -> AdapterResult:
        """Run ``code`` with the current interpreter; recorded as an action."""

        def _run() -> str:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                check=True,
            )
            return completed.stdout

        return run_action(
            self,
            run_id,
            AdapterAction(name="python", params={"code": code}, dep_scope=dep_scope),
            _run,
        )

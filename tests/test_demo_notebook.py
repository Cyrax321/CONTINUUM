"""Guards examples/demo.ipynb against rot (issue #283).

The notebook is the zero-install evaluation path (Colab, Binder), so it runs
against whatever is on main at the time. Nothing else in CI executes it, which
means a renamed API or a changed decision would break the first thing a new
reader sees, silently. This runs its code cells in order and asserts the
walkthrough still reaches the same decision points, and the same verdict, as
examples/crash_recovery_agent.py.

The cells are executed as a plain script rather than through a kernel so the
guard needs no Jupyter dependency. That only works while the notebook stays free
of line magics, which the first test enforces.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import continuum

NOTEBOOK = Path(__file__).parent.parent / "examples" / "demo.ipynb"


def _code_cells() -> list[str]:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]


def test_notebook_has_code_cells() -> None:
    assert len(_code_cells()) >= 5


def test_notebook_stays_plain_python() -> None:
    """No line magics: the cells have to run outside a kernel too."""
    for source in _code_cells():
        for line in source.splitlines():
            assert not line.lstrip().startswith(("%", "!")), f"IPython escape in a cell: {line!r}"


def test_notebook_runs_end_to_end(tmp_path: Path) -> None:
    script = tmp_path / "demo_cells.py"
    script.write_text("\n".join(_code_cells()) + "\n", encoding="utf-8")

    # Hand the child the CONTINUUM this test imported. Without it the notebook's
    # first cell would find no continuum and pip install one from GitHub, so a
    # source checkout would quietly test main instead of the working tree.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        path
        for path in (str(Path(continuum.__file__).resolve().parents[1]), env.get("PYTHONPATH"))
        if path
    )

    result = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, result.stderr

    # Whitespace-insensitive: the cells align their labels for readability.
    flat = " ".join(result.stdout.split())
    assert "exit code 9:" in flat, "the worker was not killed by os._exit(9)"
    assert "progress 400/1000 documents" in flat, "the checkpoint did not survive the kill"
    assert "mode request_human" in flat, "an unknown side effect must not resume"
    assert "continuum resume would exit 20" in flat
    assert "unresolved before 1" in flat
    assert "unresolved after 0" in flat
    assert "resumed at 400 documents" in flat, "work was reprocessed from zero"
    assert "fresh=False, external_id='481'" in flat, "the ledger re-issued a claimed action"
    assert "duplicates 0" in flat
    assert "GitHub issues created 1" in flat
    assert "progress recovered 1000/1000" in flat
    assert "No work repeated. No side effect duplicated." in result.stdout

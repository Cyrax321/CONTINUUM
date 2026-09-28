"""A missing operator-supplied input file reports an error, not a traceback (issue #1143).

The CLI renders every other operator mistake as a single ``error: ...`` line
with a nonzero exit. Four commands read a caller-named file
(``--attest``, ``--key``, ``--payload-file``) and let ``FileNotFoundError``
escape as a multi-frame traceback, because that type was not in the tuple
``main()`` catches. The path the operator mistyped is the one thing worth
printing, and a traceback buries it.
"""

from __future__ import annotations

import io
import pathlib

import pytest

from continuum.cli import main
from continuum.cli.exitcodes import ExitCode
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


@pytest.fixture
def db(tmp_path: pathlib.Path) -> str:
    path = str(tmp_path / "demo.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="Analyze 100 documents"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        store.append_event("run_1", EventType.WORK_COMPLETED, {"doc": 0})
    return path


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("argv_tail", "flag"),
    (
        (("attest-verify", "run_1", "--attest"), "--attest"),
        (("attest", "run_1", "--key"), "--key"),
        (("gate", "--run-id", "run_1", "--payload-file"), "--payload-file"),
        (("observe", "--run-id", "run_1", "--payload-file"), "--payload-file"),
    ),
)
def test_missing_input_file_is_an_error_not_a_traceback(
    db: str, tmp_path: pathlib.Path, argv_tail: tuple[str, ...], flag: str
) -> None:
    missing = tmp_path / "does-not-exist.json"

    code, out, err = run("--db", db, *argv_tail, str(missing))

    assert code == ExitCode.ERROR, f"{flag} did not fail closed: {err}"
    # The one-line contract every other operator mistake already follows.
    assert err.strip().startswith("error:")
    # The path is named, which is what the reader was looking for.
    assert str(missing) in err
    # No Python traceback: a missing file is not an internal failure.
    assert "Traceback (most recent call last)" not in err
    assert out == ""


def test_missing_file_error_quotes_the_exact_path(db: str, tmp_path: pathlib.Path) -> None:
    """The path is reported verbatim, not repr-escaped (issue #94's class of bug)."""
    missing = tmp_path / "weird name.json"

    _, _, err = run("--db", db, "attest-verify", "run_1", "--attest", str(missing))

    assert str(missing) in err

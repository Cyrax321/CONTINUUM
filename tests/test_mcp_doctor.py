"""Tests for ``continuum mcp doctor`` (issue #835).

The doctor exists for the three reproduced failure states named in the
issue: the ``mcp`` extra missing, the console script not on the host's
PATH, and a healthy install. Each state is reproduced for real -- a fake
``mcp`` package shadowing the real one on ``PYTHONPATH`` for the first, a
stripped PATH for the second -- never by mocking the checks, because the
doctor's whole value is that it observes spawned processes the way a host
does.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Any

import pytest

import continuum
from continuum.cli import ExitCode, main
from continuum.mcp import doctor
from continuum.mcp.doctor import (
    _check_handshake,
    _check_mutation_access,
    _check_resolution,
    _check_sdk,
    _handshake_command,
    _scripts_dir,
    find_registration,
    render_doctor,
    run_doctor,
)
from continuum.mcp.observation import read_observed_clients, record_observed_client

#: The ``src`` directory of whichever copy of continuum this session imported.
#: Pinned onto PYTHONPATH for the live-server tests below so the spawned child
#: imports the same code the assertions run against: a console script resolves
#: through the install's own path entry, which on a machine with several
#: checkouts can point somewhere else entirely.
SRC_DIR = Path(continuum.__file__).resolve().parents[1]

#: The venv's script directory, prepended to PATH by tests that need the
#: console script resolvable so the healthy-path assertions hold even when
#: pytest runs without the environment's bin dir on PATH. Taken from the
#: function under test rather than recomputed, so the two cannot drift
#: apart and hide the bug again (see test_scripts_dir_follows_the_venv).
SCRIPTS_DIR = _scripts_dir()

#: What a venv calls its scripts directory: ``bin`` on POSIX, ``Scripts`` on
#: Windows. Probed from this interpreter rather than hardcoded, so the
#: regression test builds a venv the platform's own sysconfig would accept.
_SCRIPTS_SUBDIR = Path(sysconfig.get_path("scripts")).name


def _with_scripts_dir_on_path() -> str:
    """A PATH that definitely contains this interpreter's console scripts."""
    return os.pathsep.join([str(SCRIPTS_DIR), os.environ.get("PATH", "")])


def _fake_missing_mcp(tmp_path: Path) -> Path:
    """A directory whose ``mcp`` package raises on import, like a missing extra.

    PYTHONPATH entries precede site-packages, so every freshly spawned
    process (the doctor's probes, and the server child itself) sees this
    shadow instead of the real SDK. That is the same goal as the
    ``sys.meta_path`` blocker in ``tests/test_mcp_server.py``, but it works
    across process boundaries, which is what the doctor's fresh-subprocess
    probes require.
    """
    package = tmp_path / "shadow" / "mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'raise ModuleNotFoundError("No module named \'mcp\'", name="mcp")\n',
        encoding="utf-8",
    )
    return package.parent


def _by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the report's checks by name."""
    return {check["check"]: check for check in report["checks"]}


def test_healthy_install_completes_a_real_handshake(monkeypatch: Any) -> None:
    """The acceptance case: doctor exits 0 and reports all tools on a good install.

    PATH is pinned to include the interpreter's script dir so the assertion
    holds wherever pytest runs; the handshake is still real -- a spawned
    server answering ``initialize`` and ``tools/list`` over stdio.
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    report = run_doctor()

    assert report["healthy"] is True
    checks = _by_name(report)
    assert checks["sdk-import"]["status"] == "pass"
    assert checks["command-resolution"]["status"] == "pass"
    handshake = checks["handshake"]
    assert handshake["status"] == "pass"
    assert handshake["server"] == "continuum-mcp"
    # "reports all tools": the count matches what the docs table guards.
    assert len(handshake["tools"]) >= 12
    assert "continuum_record_progress" in handshake["tools"]


def test_missing_extra_names_the_cause_and_the_fix(monkeypatch: Any, tmp_path: Path) -> None:
    """A missing SDK must be diagnosed, not reported as a bare CONNECTION_CLOSED.

    The handshake failure must carry the server's own stderr tail, because
    that message ("install it with: pip install continuum-agent[mcp]") is
    the one thing the host never shows the user (issue #87/#93, #697).
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(_fake_missing_mcp(tmp_path)), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )

    report = run_doctor()

    assert report["healthy"] is False
    checks = _by_name(report)
    assert checks["sdk-import"]["status"] == "fail"
    assert "pip install continuum-agent[mcp]" in checks["sdk-import"]["fix"]
    handshake = checks["handshake"]
    assert handshake["status"] == "fail"
    assert "continuum-agent[mcp]" in handshake["detail"], (
        "the child's stderr names the fix; the doctor must surface it"
    )


def test_exe_not_on_host_path_is_a_failure_state(monkeypatch: Any, tmp_path: Path) -> None:
    """A script the host cannot spawn is a failure even when the code works.

    The diagnosis must name the scripts directory that is missing from PATH,
    because that directory is the fix. The handshake still runs against the
    ``python -m continuum.mcp`` fallback: proving the module form works is
    what separates "broken install" from "broken PATH".
    """
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    (tmp_path / "empty-path").mkdir()

    report = run_doctor()

    assert report["healthy"] is False
    checks = _by_name(report)
    resolution = checks["command-resolution"]
    assert resolution["status"] == "fail"
    assert "not on the PATH" in resolution["detail"]
    assert str(SCRIPTS_DIR) in resolution["fix"], "the fix names the directory to add"
    # The fallback handshake succeeding is the useful extra fact, not a pass.
    assert checks["handshake"]["status"] == "pass"
    assert checks["handshake"]["command"] == [sys.executable, "-u", "-m", "continuum.mcp"]


def test_scripts_dir_follows_the_venv_not_the_symlink(monkeypatch: Any, tmp_path: Path) -> None:
    r"""A venv python is a symlink, and resolving it escapes the venv.

    ``Path(sys.executable).resolve().parent`` on a venv returns the base
    interpreter's ``bin``, which holds neither the script nor the directory
    the operator should add to PATH. Windows has the same shape with
    ``Scripts`` beside the executable. The diagnosis has to name the venv's
    own directory, so a script installed there is what the fix points at.

    What is built for real is the failure itself: a python that is a symlink
    into this interpreter, installed in the venv's own scripts directory.
    Resolving it leaves the venv -- the premise asserted below -- and that
    is the directory the old logic would have reported.

    What is stubbed is the venv's *identity*, and only that. A venv is
    established at interpreter startup, before ``sysconfig`` reads the
    prefix, so repointing ``sys.prefix`` in an already-running interpreter
    cannot reproduce one (3.13 happens to read it dynamically, 3.11 does
    not, and the difference is not a property worth depending on). The
    venv's install location is therefore supplied to ``sysconfig`` directly,
    which is the question the diagnosis actually asks of it: where does this
    install put its scripts? The answer is the venv's own directory, never
    the symlink's target.
    """
    venv_bin = tmp_path / "venv" / _SCRIPTS_SUBDIR
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(sys.executable)

    monkeypatch.setattr(sys, "executable", str(venv_python))
    monkeypatch.setattr(
        sysconfig, "get_path", lambda name, **_: str(venv_bin) if name == "scripts" else None
    )

    # The premise: resolving the venv python leaves the venv, which is the
    # bug the old logic would have baked into the diagnosis.
    assert venv_python.resolve().parent != venv_bin

    assert _scripts_dir() == venv_bin


def test_the_probe_reports_the_wire_framing_it_observed(monkeypatch: Any) -> None:
    """Framing is reported as observed, not assumed by platform.

    The CRLF bug is upstream and Windows-only today (#839); a doctor that
    guessed "CRLF on win32" would go stale the day the SDK fixes it. What is
    invariant is that the note says what the probe actually saw, and that
    seeing CRLF does not flip the verdict on its own.
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    report = run_doctor()

    notes = [check for check in report["checks"] if check["check"] == "wire-framing"]
    assert notes, "the probe observed framing, so a note must be reported"
    observed = next(
        note for note in notes if note["detail"].startswith("response frames end with ")
    )
    expected = "CRLF (\\r\\n)" if sys.platform == "win32" else "LF (\\n)"
    assert expected in observed["detail"], (
        "the note names the terminator this platform's wire actually uses, "
        "not the one a platform guess would have printed"
    )
    # A framing note must never flip the verdict by itself, not even the warn
    # the Windows CRLF finding raises.
    assert report["healthy"] is True


def test_cli_json_output_and_exit_codes(monkeypatch: Any, tmp_path: Path) -> None:
    """``--json`` emits the machine-readable report; exit codes follow the verdict.

    The command must also not create a database as a side effect: it probes
    with a throwaway one, and the cwd must stay clean wherever it runs.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    out = io.StringIO()
    code = main(["--json", "mcp", "doctor"], out=out, err=io.StringIO())

    assert code == ExitCode.OK
    payload = json.loads(out.getvalue())
    assert payload["healthy"] is True
    assert {check["check"] for check in payload["checks"]} >= {
        "sdk-import",
        "command-resolution",
        "handshake",
    }
    assert not list(tmp_path.glob("*.db")), "doctor must not create a database in cwd"

    # And the rendered text names the check and its fix on the failure path.
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(_fake_missing_mcp(tmp_path)), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    )
    out = io.StringIO()
    code = main(["mcp", "doctor"], out=out, err=io.StringIO())

    assert code == ExitCode.ERROR
    assert "pip install continuum-agent[mcp]" in out.getvalue()


def test_an_unusable_timeout_is_rejected_before_any_diagnosis(monkeypatch: Any) -> None:
    """A deadline that is not a usable wait is a wrong diagnosis, not a slow one.

    The timeout bounds every probe, not just the handshake reads, so zero or a
    negative value means the probes give up before they start: a healthy
    install is reported as entirely broken, with every check failing including
    the import and PATH probes that involve no waiting at all. That is the
    inverse of what the doctor is for, and it is silent -- nothing in the
    report hints that the deadline was the cause, so the operator chases a
    phantom broken install. Refusing the value at parse time names the flag
    instead, and ``nan`` / ``inf`` are the same class of unusable wait.
    """
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    for bad in ("0", "-1", "nan", "inf"):
        err = io.StringIO()
        with monkeypatch.context() as capture, pytest.raises(SystemExit) as raised:
            # argparse's own error path writes to ``sys.stderr``, not the
            # stream ``main`` is handed.
            capture.setattr(sys, "stderr", err)
            main(["mcp", "doctor", "--timeout", bad], out=io.StringIO(), err=io.StringIO())

        assert raised.value.code == 2, bad
        assert "--timeout" in err.getvalue(), bad
        assert bad in err.getvalue(), bad
        # Nothing was diagnosed on the way out.
        assert "handshake" not in err.getvalue(), bad

    # A usable value still reaches the doctor and exits on the verdict.
    out = io.StringIO()
    code = main(["--json", "mcp", "doctor", "--timeout", "20"], out=out, err=io.StringIO())

    assert code == ExitCode.OK
    assert json.loads(out.getvalue())["healthy"] is True


def test_render_doctor_is_one_line_per_finding(monkeypatch: Any, tmp_path: Path) -> None:
    """Every check renders exactly one actionable line, plus its fix when failing.

    The issue's wording is "one actionable line per finding"; a check whose
    finding never reaches the output is a diagnosis the user cannot see.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    (tmp_path / "empty-path").mkdir()

    text = render_doctor(run_doctor())

    assert "[fail] command-resolution:" in text
    assert "fix:" in text
    assert "[ok]   handshake:" in text  # module fallback still works
    assert "not healthy" in text
    assert "remedies:" in text


def test_the_handshake_reads_frames_without_select_on_pipes(monkeypatch: Any) -> None:
    """Windows ``select()`` accepts sockets only, so the read cannot use it.

    Registering a child's stdout pipe with a selector raises ``WinError
    10038`` on Windows, and every probe died before the first frame arrived
    -- all six tests in this module failed there, on code that passed
    everywhere else. This makes that failure deterministic on POSIX: if the
    reader depends on a selector the handshake never completes, and the
    healthy-install assertion below cannot hold.
    """
    import selectors

    def _windows_like_selector() -> Any:
        raise OSError(10038, "An operation was attempted on something that is not a socket")

    monkeypatch.setattr(selectors, "DefaultSelector", _windows_like_selector)
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())

    report = run_doctor()

    assert report["healthy"] is True
    handshake = _by_name(report)["handshake"]
    assert handshake["status"] == "pass"
    assert "continuum_record_progress" in handshake["tools"]


#: The protocol each fake server below speaks: read the request, answer it.
#: ``-c`` body, so the command is this interpreter on every platform -- no
#: PATH lookup, no extension, no shebang.
def _fake_server(body: str) -> list[str]:
    """An argv whose stdout speaks whatever protocol failure ``body`` enacts."""
    return [sys.executable, "-c", body]


def _check(command: list[str] | None, timeout: float = 5.0) -> tuple[Any, list[Any]]:
    finding, notes = _check_handshake(command, timeout)
    return finding, notes


def test_the_handshake_reports_a_server_that_answers_with_an_error() -> None:
    """A JSON-RPC error is a server that is up, and the message is the detail.

    ``CONNECTION_CLOSED`` is not the only way a spawn fails; a server that
    answers with an error object would otherwise surface as a bare
    "never completed the handshake", burying the message that names the cause.
    """
    body = (
        "import json, sys\n"
        "req = json.loads(sys.stdin.readline())\n"
        'sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], '
        '"error": {"code": -32601, "message": "method not allowed"}}) + "\\n")\n'
        "sys.stdout.flush()\n"
    )
    finding, _ = _check(_fake_server(body))

    assert finding["status"] == "fail"
    assert "server returned an error: method not allowed" in finding["detail"]


def test_the_handshake_names_an_unparseable_response() -> None:
    """A line that is not JSON is reported as wire corruption, not silence.

    The framing probe's whole point is that the wire is observed byte for
    byte; a server that logs to stdout (a common misconfiguration) sends
    exactly this shape.
    """
    body = (
        "import json, sys\n"
        "req = json.loads(sys.stdin.readline())\n"
        'sys.stdout.write("this line is not json\\n")\n'
        "sys.stdout.flush()\n"
    )
    finding, _ = _check(_fake_server(body))

    assert finding["status"] == "fail"
    assert "unparseable response" in finding["detail"]


def test_the_handshake_gives_up_on_a_server_that_never_answers() -> None:
    """A wedged server is bounded by the deadline, then killed by close().

    Both timeouts in the read path have to fire for this to be a diagnosis
    rather than a hang: the per-chunk deadline (``queue.get``) and the
    whole-read deadline (``remaining <= 0``). And ``close`` must not join a
    server that ignores a closed stdin -- it kills it, or the probe leaks a
    process per diagnosis.
    """
    finding, _ = _check(_fake_server("import time\ntime.sleep(120)\n"), timeout=1.0)

    assert finding["status"] == "fail"
    assert "never completed the initialize handshake" in finding["detail"]


def test_the_handshake_keeps_stderr_written_after_the_read_deadline() -> None:
    """A cause printed past the deadline still reaches the report.

    The read deadline firing does not mean the child is done with its
    diagnosis: a server may spend the deadline window on startup and only
    write its cause as it gives up. ``close`` terminates it and joins the
    stderr reader, and the tail is read after that -- snapshotting at the
    deadline instead would name a timeout where the child's own message was
    a moment away, which is the opaque failure the doctor exists to remove.
    """
    body = (
        "import sys, time\n"
        "time.sleep(2)\n"  # past the read deadline, but before close() kills it
        "sys.stderr.write('the real cause: mcp extra missing\\n')\n"
        "sys.stderr.flush()\n"
    )
    finding, _ = _check(_fake_server(body), timeout=0.4)

    assert finding["status"] == "fail"
    assert "never completed the initialize handshake" in finding["detail"]
    assert "the real cause: mcp extra missing" in finding["detail"], (
        "the child wrote its cause after the deadline and close() must "
        "still drain it into the report"
    )


def test_the_handshake_attributes_a_server_that_exits_as_it_gives_up() -> None:
    """A dying child's exit code reaches the report, not just its timeout (issue #835).

    The read deadline fires the instant stdout goes quiet, and a server that
    is dying -- a fast-failing one closes stdout and exits within
    milliseconds, a slow one holds both open until it gives up -- is often
    still alive in that instant, before the OS has reaped it. An immediate
    ``poll()`` then reports a live process for a server that is already
    going, which hides the exit code that distinguishes ``CONNECTION_CLOSED``
    from a server that is genuinely wedged, and with it the stderr tail that
    names the cause. That is the failure the doctor exists to diagnose.
    ``close`` waits for the child to terminate before the failure is
    reported, so both are read from a process that is done.

    This makes the window deterministic rather than hoping a fast child loses
    the race: the child is alive when the deadline fires and exits while
    ``close`` waits, which is the same code path a millisecond-scale failure
    takes, on any machine speed.
    """
    body = (
        "import sys, time\n"
        "time.sleep(1.5)\n"
        "sys.stderr.write('error: the MCP server needs the optional mcp "
        "dependency\\n')\n"
        "sys.exit(3)\n"
    )
    finding, _ = _check(_fake_server(body), timeout=1.0)

    assert finding["status"] == "fail"
    # If poll() ran before close() waited for the child, it would be None at
    # the deadline and this clause would be unreachable: the report would say
    # only "no response within the deadline".
    assert "exited with code 3 before the handshake" in finding["detail"]
    assert "this is what the host reports as CONNECTION_CLOSED" in finding["detail"]
    assert "the MCP server needs the optional mcp dependency" in finding["detail"]


def test_a_frame_coalesced_with_a_notification_is_not_lost() -> None:
    """Bytes past the first newline belong to the next frame (issue #835).

    A server commonly writes a notification and the response back to back,
    and one ``os.read`` then returns both at once, because a pipe coalesces
    whatever was written between reads. ``_read_line`` split on the first
    newline and threw the rest away, so the notification's frame was dropped
    and the response after it never arrived: the report said the server never
    answered one that had. The surplus is kept in an inbox for the next read.

    The fake server writes the notification and the answer in a single
    ``sys.stdout.write`` with one flush, which makes one read deliver both on
    every platform and at any machine speed -- no timing to win or lose. The
    notification is skipped by ``request`` (its id is not ours), which is what
    makes the dropped frame fatal: the answer it discards is the only one sent.
    """
    body = (
        "import json, sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request:\n"
        "        continue  # the client's own notification, nothing to answer\n"
        "    notification = json.dumps(\n"
        "        {'jsonrpc': '2.0', 'method': 'notifications/message',\n"
        "         'params': {'level': 'info'}}\n"
        "    )\n"
        "    if request['method'] == 'initialize':\n"
        "        result = {'serverInfo': {'name': 'coalesced', 'version': '1'},\n"
        "                  'protocolVersion': '2024-11-05', 'capabilities': {}}\n"
        "    else:\n"
        "        result = {'tools': [{'name': 'continuum_ping'}]}\n"
        "    answer = json.dumps(\n"
        "        {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    )\n"
        "    # One write, one flush: one os.read returns both frames.\n"
        "    sys.stdout.write(notification + '\\n' + answer + '\\n')\n"
        "    sys.stdout.flush()\n"
    )
    finding, _ = _check(_fake_server(body), timeout=3.0)

    assert finding["status"] == "pass", finding.get("detail")
    assert finding["server"] == "coalesced"
    assert finding["tools"] == ["continuum_ping"]


def test_the_handshake_reports_a_command_that_cannot_spawn(tmp_path: Path) -> None:
    """No spawnable command is itself a finding, not an exception.

    ``_handshake_command`` returns None when resolution found neither a
    script nor the module fallback -- the doctor's job here is to say so
    plainly instead of crashing on the None it produced.
    """
    finding, _ = _check(None)

    assert finding["status"] == "fail"
    assert "no spawnable command to probe" in finding["detail"]
    assert "pip install continuum-agent[mcp]" in finding["fix"]


def test_the_handshake_reports_an_unspawnable_executable(tmp_path: Path) -> None:
    """An executable that does not exist is ``OSError``, not a traceback.

    The command came from a stale registration, which is exactly the state
    issue #841 says the doctor should name a remedy for.
    """
    missing = tmp_path / "no-such-server"
    finding, _ = _check([str(missing)])

    assert finding["status"] == "fail"
    assert "spawning" in finding["detail"]
    assert str(missing) in finding["detail"]


def test_resolution_distinguishes_not_on_path_from_not_installed(monkeypatch: Any) -> None:
    """The same failure names a different fix depending on one file.

    ``continuum-mcp`` off PATH is fixed by adding the directory; the script
    absent from the interpreter's own scripts dir is fixed by reinstalling.
    The diagnosis has to tell them apart because the operator cannot.
    """
    monkeypatch.setattr(
        doctor,
        "_run_probe",
        lambda code, **_: (0, '{"which": null, "module": true, "path": "/x"}', ""),
    )

    for exists, remedy in (
        (True, "add"),
        (False, "reinstall with"),
    ):
        monkeypatch.setattr(doctor, "_script_exists_next_to_interpreter", lambda e=exists: e)
        resolution = _check_resolution(5.0)
        assert resolution["status"] == "fail", exists
        assert remedy in resolution["fix"], exists


def test_a_probe_that_times_out_is_a_failure_not_a_hang(monkeypatch: Any) -> None:
    """A probe past its deadline becomes a finding, never an infinite wait.

    ``shutil.which`` walks PATH in the child; a PATH with a loop or a huge
    network mount makes both probes slow, which is why the remedy names it.
    """

    def _hang(code: str, **_: Any) -> tuple[int, str, str]:
        raise TimeoutExpired(cmd=[sys.executable, "-c", code], timeout=1.0)

    monkeypatch.setattr(doctor, "_run_probe", _hang)

    sdk = _check_sdk(1.0)
    assert sdk["status"] == "fail"
    assert "timed out" in sdk["detail"]

    resolution = _check_resolution(1.0)
    assert resolution["status"] == "fail"
    assert "timed out" in resolution["detail"]
    assert "PATH" in resolution["fix"]


def test_a_probe_that_fails_is_reported_as_such(monkeypatch: Any) -> None:
    """A resolution probe that dies cannot answer, so the check says so."""
    monkeypatch.setattr(doctor, "_run_probe", lambda code, **_: (1, "", "boom"))

    resolution = _check_resolution(5.0)
    assert resolution["status"] == "fail"
    assert "boom" in resolution["detail"]


def test_the_handshake_command_follows_resolution() -> None:
    """Resolution's two answers map to the two commands, and its failure to none.

    A pure function over the resolution finding: pinning it keeps the
    wiring between the two checks from silently changing shape.
    """
    assert _handshake_command({"resolved": "/opt/bin/continuum-mcp"}) == ["/opt/bin/continuum-mcp"]
    assert _handshake_command({"resolved": None, "module_fallback": True}) == [
        sys.executable,
        "-u",
        "-m",
        "continuum.mcp",
    ]
    assert _handshake_command({"resolved": None, "module_fallback": False}) is None


# --------------------------------------------------------------------------- #
# the baked client name, and whether the server honors it
# --------------------------------------------------------------------------- #


def _isolated_home(monkeypatch: Any, tmp_path: Path) -> Path:
    """Point ``~`` at an empty directory so only the cwd can hold a registration.

    User and local scope live under the home directory, so on any machine that
    has run ``continuum mcp install`` they would otherwise answer the
    "absent registration" case and make it depend on whose account the tests
    run as. Both spellings are set because ``expanduser`` reads ``HOME`` on
    POSIX and ``USERPROFILE`` on Windows.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _write_registration(
    directory: Path, baked: str | None, *, host_file: str = ".mcp.json"
) -> Path:
    """A project-scope registration in ``directory``, baking ``baked``.

    Shaped like the entry ``mcp install`` writes: an absolute command, an
    absolute ``--db`` and the client name in the environment. ``baked=None``
    writes an entry with no allowlist at all, which is what a registration
    somebody edited by hand tends to look like.
    """
    entry: dict[str, Any] = {
        "type": "stdio",
        "command": str(SCRIPTS_DIR / "continuum-mcp"),
        "args": ["--db", str(directory / "continuum.db")],
    }
    if baked is not None:
        entry["env"] = {"CONTINUUM_MCP_MUTATING_CLIENTS": baked}
    path = directory / host_file
    path.write_text(
        json.dumps({"mcpServers": {"continuum-mcp": entry}}, indent=2), encoding="utf-8"
    )
    return path


def _answering_server(answer: str) -> list[str]:
    """A server that handshakes, then answers ``tools/call`` with ``answer``.

    The tool result carries ``isError`` exactly as the real SDK reports a
    refusal, because that is the shape the permission check has to read: a
    refused tool is a *result*, not a JSON-RPC error, so a probe that only
    looked for ``error`` frames would call every caller authorized.
    """
    body = (
        "import json, sys\n"
        "def send(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request:\n"
        "        continue  # notifications get silence, like the real server\n"
        "    if request['method'] == 'initialize':\n"
        "        result = {'serverInfo': {'name': 'fake', 'version': '1'},\n"
        "                  'protocolVersion': '2024-11-05', 'capabilities': {}}\n"
        "    elif request['method'] == 'tools/call':\n"
        "        result = {'content': [{'type': 'text', 'text': " + repr(answer) + "}],\n"
        "                  'isError': True}\n"
        "    else:\n"
        "        result = {}\n"
        "    send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})\n"
    )
    return _fake_server(body)


def _observe(directory: Path, *names: str) -> None:
    """Record what host connections declared against a written registration.

    Stands in for real hosts having connected through this project's
    database, which is the only way the doctor learns a name the registration
    did not already predict.
    """
    for name in names:
        record_observed_client(str(directory / "continuum.db"), name)


def _observe_via_a_real_connection(database: Path, client_name: str, allow: str) -> str:
    """Spawn the real server, connect as ``client_name``, and leave its record.

    The other observation helpers write the file directly. This one goes
    through a real stdio handshake so the end-to-end claim holds: the value
    the doctor reads is the one the server itself took off the wire.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    env["CONTINUUM_MCP_MUTATING_CLIENTS"] = allow
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "continuum.mcp", "--db", str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None

    def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1.0"},
            },
        }
    )
    assert json.loads(process.stdout.readline())["result"]
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "continuum_record_progress",
                "arguments": {"run_id": "no-such-run-xyz", "completed": 1},
            },
        }
    )
    answer = json.loads(process.stdout.readline())
    process.stdin.close()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - the server always exits here
        process.kill()
    return answer["result"]["content"][0]["text"]


def test_a_real_host_connection_is_what_the_doctor_ends_up_reporting(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The whole chain, with nothing simulated: a host connects, the doctor sees it.

    The registration bakes ``cursor``, the same command the host would run. A
    real Cursor-style connection declares ``cursor-vscode``, the server records
    it and refuses the call, and the doctor then reports the two names against
    each other. Every other test in this area writes one side of that directly;
    this one has the server take the name off the wire.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    registration = find_registration(tmp_path)
    assert registration is not None

    # The host connects for real and is refused, because install baked the
    # wrong name for it. That refusal is the whole silent failure.
    refused = _observe_via_a_real_connection(
        tmp_path / "continuum.db", "cursor-vscode", allow="cursor"
    )
    assert "is not permitted to use the mutating tool" in refused, refused

    client_name, _ = _check_mutation_access(_live_server(), registration, 20.0)

    assert client_name["status"] == "warn"
    assert client_name["baked_client"] == "cursor"
    assert client_name["observed_clients"] == ["cursor-vscode"]
    assert "cursor-vscode" in client_name["detail"]


def _live_server() -> list[str]:
    """The real server, resolved the way a host without a script on PATH gets it."""
    return [sys.executable, "-u", "-m", "continuum.mcp"]


def test_the_baked_client_name_is_reported_and_verified(monkeypatch: Any, tmp_path: Path) -> None:
    """A registration that grants mutation is reported as doing exactly that.

    This is the acceptance case for the check: the operator reads which client
    name the registration bakes, a live server agrees that name may mutate,
    and a real host has connected under that same name. Nothing here inspects
    the policy source; the verdict comes from a guarded tool actually being
    reached over the wire.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    _observe(tmp_path, "cursor")
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    registration = find_registration(tmp_path)
    assert registration is not None
    assert registration["baked_client"] == "cursor"

    client_name, permission = _check_mutation_access(_live_server(), registration, 20.0)

    assert client_name["status"] == "pass", client_name["detail"]
    assert client_name["baked_client"] == "cursor"
    assert client_name["observed_client"] == "cursor"
    assert client_name["observed_clients"] == ["cursor"]
    assert permission["status"] == "pass", permission["detail"]
    assert permission["authorized"] is True
    assert permission["tool"] == "continuum_record_progress"


def test_a_host_name_that_differs_from_the_baked_name_is_a_warning(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The bug, non-tautologically: the two strings come from different places.

    The registration bakes ``cursor`` and a real host connected as
    ``cursor-vscode``. Neither value can be derived from the other: the baked
    name comes from the registration file, the observed one from the server's
    own record of a connection the doctor had no part in. Before the server
    kept that record, this check could only compare the baked name with
    itself and reported pass whatever the host would really send.

    It warns rather than fails on purpose. The observed name is asserted by
    whoever connected, so treating it as proof of anything but a mismatch
    would let any client claim to be a host and have the doctor agree.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    _observe(tmp_path, "cursor-vscode")
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    registration = find_registration(tmp_path)

    client_name, permission = _check_mutation_access(_live_server(), registration, 20.0)

    assert client_name["status"] == "warn"
    assert client_name["baked_client"] == "cursor"
    assert client_name["observed_client"] == "cursor-vscode"
    assert client_name["observed_clients"] == ["cursor-vscode"]
    assert "cursor-vscode" in client_name["detail"] and "cursor" in client_name["detail"]
    assert "CONTINUUM_MCP_MUTATING_CLIENTS" in client_name["fix"]
    # The authorized side is unaffected by what was observed.
    assert permission["authorized"] is True


def test_nothing_observed_is_a_warning_rather_than_a_pass(monkeypatch: Any, tmp_path: Path) -> None:
    """Before any host connects, agreement is unverified and must read as such.

    The probe declares the name read from the registration, and that same
    registration seeds the allowlist the probe runs against, so with no
    observation the check compares the baked name with itself. Calling that a
    pass would claim a verification that never happened, which is how the
    original bug stayed invisible.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    registration = find_registration(tmp_path)

    client_name, permission = _check_mutation_access(_live_server(), registration, 20.0)

    assert client_name["status"] == "warn"
    assert client_name["observed_clients"] == []
    assert "no host has connected" in client_name["detail"]
    assert permission["status"] == "pass", "the authorized side was still verified"


def test_an_observed_name_can_never_rescue_a_refused_caller(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A recorded name does not authorize, even when it is the allowlisted one.

    The registration names ``cursor`` but grants nothing the server accepts,
    so the live call is refused. The observation file now says a host
    connected as ``cursor``. The finding must stay a failure: letting the
    observation override a refusal would hand authorization to a file any
    client can write by connecting.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    _observe(tmp_path, "cursor")
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    registration = find_registration(tmp_path)
    # An empty env in the registration: the file names a client, but the
    # server is handed no allowlist at all, so default-deny applies.
    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["continuum-mcp"]
    entry.pop("env")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"continuum-mcp": entry}}), encoding="utf-8"
    )
    assert find_registration(tmp_path) is None, "without an env the entry names no client"

    # With the name still in the file but the allowlist emptied out, the
    # observed name must not carry the check.
    registration = dict(registration or {}, baked_client="cursor")
    client_name, permission = _check_mutation_access(_live_server(), registration, 20.0)

    assert permission["authorized"] is False
    assert client_name["status"] in ("fail", "warn")
    assert client_name["status"] != "pass"


def test_the_doctor_reads_observations_beside_the_registration_database(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The observations come from the host's database, not the probe's.

    The doctor probes against a throwaway database, so if it read or wrote that
    one it would only ever see the name it declared itself. The ``--db`` the
    registration points the host at names the project directory real
    connections open, so that is where the record is read from and where the
    real history has to survive the probe.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    _observe(tmp_path, "cursor-vscode")
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    registration = find_registration(tmp_path)
    assert registration is not None
    assert registration["database"] == str(tmp_path / "continuum.db")

    client_name, _ = _check_mutation_access(_live_server(), registration, 20.0)

    assert client_name["observed_clients"] == ["cursor-vscode"], (
        "the probe declared 'cursor'; if it had written its own handshake into "
        "the project's record, that name would now be in there too"
    )
    assert sorted(read_observed_clients(str(tmp_path / "continuum.db"))) == ["cursor-vscode"]


def test_a_registration_the_server_refuses_fails_and_names_both_strings(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The whole point: a refused caller must be loud, and name both strings.

    The registration bakes one client name and the server refuses it in favour
    of another. Reporting only one of the two leaves the operator with no way to
    tell which string to change, which is how a silently read-only agent stays
    unread-only. The refusal text below is the live server's own wording,
    captured from a real refused call rather than invented here.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor-vscode")
    registration = find_registration(tmp_path)
    refusal = (
        "Error executing tool continuum_record_progress: caller 'cursor-vscode' is not "
        "permitted to use the mutating tool 'continuum_record_progress'. Permitted "
        "callers: cursor. Read-only tools remain available."
    )

    client_name, permission = _check_mutation_access(_answering_server(refusal), registration, 20.0)

    assert client_name["status"] == "fail"
    assert "cursor-vscode" in client_name["detail"], "names what the registration bakes"
    assert "cursor" in client_name["detail"], "names what the server permits instead"
    assert client_name["baked_client"] == "cursor-vscode"
    assert client_name["permitted_clients"] == "cursor"
    assert "continuum mcp install" in client_name["fix"]
    assert permission["status"] == "fail"
    assert permission["authorized"] is False
    assert "CONTINUUM_MCP_MUTATING_CLIENTS" in client_name["fix"]


@pytest.mark.parametrize(
    ("answer", "authorized", "status"),
    [
        pytest.param(
            "Error executing tool continuum_record_progress: no such run: "
            "'continuum-mcp-doctor-no-such-run'",
            True,
            "pass",
            id="authorized-then-failed-downstream",
        ),
        pytest.param(
            "Error executing tool continuum_record_progress: caller 'cursor' is not permitted "
            "to use the mutating tool 'continuum_record_progress'. Permitted callers: other.",
            False,
            "fail",
            id="refused-as-unauthorized",
        ),
    ],
)
def test_authorized_but_failed_downstream_is_not_the_same_as_refused(
    monkeypatch: Any, tmp_path: Path, answer: str, authorized: bool, status: str
) -> None:
    """Both answers are ``isError`` results; only one of them is degradation.

    The SDK reports a tool that refused a call as a result carrying ``isError``,
    not as a JSON-RPC error frame, and the two situations this check must
    separate arrive through that same door. A call that got past authorization
    and then failed on its own arguments proves the caller may mutate; a
    refusal proves the agent has silently lost its mutating tools. Collapse the
    two, or read every ``isError`` as a refusal, and the original bug hides
    behind a healthy-looking report.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, "cursor")
    registration = find_registration(tmp_path)

    _, permission = _check_mutation_access(_answering_server(answer), registration, 20.0)

    assert permission["authorized"] is authorized, permission["detail"]
    assert permission["status"] == status
    assert permission["server_said"] == answer, "the server's own words reach the report"


def test_an_absent_registration_warns_and_names_the_next_command(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Nothing installed is a warn, not a fail, and must say what to run.

    Failing here would be wrong twice over: there is no broken install to
    report, and the operator who has never run the installer is exactly who
    needs the command. The permission probe is skipped rather than run against
    a name nobody declared, because default-deny would make an unconfigured
    machine look like a degraded one.
    """
    _isolated_home(monkeypatch, tmp_path)
    assert find_registration(tmp_path) is None

    client_name, permission = _check_mutation_access(_live_server(), None, 20.0)

    assert client_name["status"] == "warn"
    assert "continuum mcp install" in client_name["fix"]
    assert client_name["baked_client"] is None
    assert permission["status"] == "info"
    assert permission["authorized"] is None
    assert "not probed" in permission["detail"]


def test_an_absent_registration_leaves_the_report_healthy(monkeypatch: Any, tmp_path: Path) -> None:
    """A warn must not flip the verdict, and must reach the rendered report.

    ``run_doctor`` reports healthy when nothing failed. An unwarned warn would
    be a finding the operator never reads, which is the silent failure this
    check was added to remove, so the warning has to survive into the text.
    """
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", _with_scripts_dir_on_path())
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))

    report = run_doctor()

    checks = _by_name(report)
    assert checks["client-name"]["status"] == "warn"
    assert report["healthy"] is True, "an absent registration is not a broken install"
    text = render_doctor(report)
    assert "[warn] client-name:" in text
    assert "continuum mcp install" in checks["client-name"]["fix"]
    # The command has to survive into the rendered text, not only the payload:
    # a healthy verdict suppresses the remedies block, so the fix line printed
    # under the warning is the only place the operator will ever see it.
    assert "continuum mcp install" in text
    assert any("continuum mcp install" in remedy for remedy in report["remedies"])


def test_the_live_server_refuses_a_name_the_registration_did_not_bake(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The reproduced degradation, against the real server: no IDE required.

    The registration's environment grants mutation to ``cursor``. The host
    declares ``cursor-vscode`` in its handshake, which is what the server
    observes as the caller. The server refuses, and the agent is left with the
    three read-only tools while install reported success and every other check
    passed. The registration handed to the check is built by hand precisely
    because it models what discovery alone cannot see: the name the host sends
    and the name the registration grants are two different strings.
    """
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    settings_path = _write_registration(tmp_path, "cursor")
    registration = {
        "host": "cursor",
        "settings_path": settings_path,
        "scope": "project",
        "project_root": tmp_path,
        "baked_client": "cursor-vscode",
    }

    client_name, permission = _check_mutation_access(_live_server(), registration, 20.0)

    assert client_name["status"] == "fail", client_name["detail"]
    assert client_name["baked_client"] == "cursor-vscode"
    assert client_name["permitted_clients"] == "cursor"
    assert permission["authorized"] is False
    assert "not permitted" in permission["server_said"]
    # Both strings are in the finding, because the operator has to choose one.
    assert "cursor-vscode" in client_name["detail"] and "cursor" in client_name["detail"]


def test_a_registration_with_no_client_name_is_reported_as_absent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """An entry with no allowlist names no caller, so it is the absent case.

    A registration can exist and still grant nobody anything: the environment
    block is the only place the name lives, and dropping it (a hand edit, a
    merge, a host that ignores ``env``) leaves a registration that installs
    cleanly and authorizes no one. Reading it as "no client is baked" is what
    makes that state a warning with a fix rather than a silent pass.
    """
    _isolated_home(monkeypatch, tmp_path)
    _write_registration(tmp_path, None)

    assert find_registration(tmp_path) is None

    client_name, permission = _check_mutation_access(_live_server(), None, 20.0)

    assert client_name["status"] == "warn"
    assert permission["status"] == "info"


def test_a_stale_baked_path_is_still_caught_and_the_new_checks_do_not_mask_it() -> None:
    """A registration pointing at a moved virtualenv still fails the handshake.

    The new checks only run once the handshake has passed, so they cannot
    replace the diagnosis that names an unspawnable command. A stale baked path
    must still be reported by name, which is the state a moved venv leaves and
    the one the operator has to act on.
    """
    stale = "/nonexistent/venv/bin/continuum-mcp"
    finding, _ = _check([stale])

    assert finding["status"] == "fail"
    assert "spawning" in finding["detail"]
    assert stale in finding["detail"]


def test_the_probe_environment_does_not_carry_the_policy_in(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The verdict must reflect the registration, not the operator's shell.

    A ``CONTINUUM_MCP_MUTATING_CLIENTS`` exported in the terminal running the
    doctor would otherwise authorize a registration that grants nothing, which
    is precisely the silent failure this check exists to end. PATH and
    PYTHONPATH are left alone, because the child still has to be spawnable and
    importable for the call to say anything.
    """
    _isolated_home(monkeypatch, tmp_path)
    settings_path = _write_registration(tmp_path, "cursor")
    registration = {
        "host": "cursor",
        "settings_path": settings_path,
        "scope": "project",
        "project_root": tmp_path,
        "baked_client": "cursor",
    }
    monkeypatch.setenv("CONTINUUM_MCP_MUTATING_CLIENTS", "ambient-shell-name")
    monkeypatch.setenv("CONTINUUM_MCP_ALLOW", "ambient-shell-name")

    env = doctor._probe_environment(registration)

    assert (
        "CONTINUUM_MCP_MUTATING_CLIENTS" not in env
        or env["CONTINUUM_MCP_MUTATING_CLIENTS"] == "cursor"
    ), "the ambient value must not survive into the probe"
    assert env["CONTINUUM_MCP_MUTATING_CLIENTS"] == "cursor", "the registration's own value does"
    assert env.get("PATH"), "the child still has to be spawnable"


def test_a_settings_file_the_doctor_cannot_parse_is_not_a_crash(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Discovery runs on machines that are already broken; it must not raise.

    The doctor is asked to run precisely when something is wrong, and a
    settings file with a stray comma in it is a finding about the registration,
    not an exception out of the diagnosis. Every unreadable shape resolves to
    "no registration here" so the search continues to the next candidate.
    """
    _isolated_home(monkeypatch, tmp_path)
    (tmp_path / ".mcp.json").write_text("{not json at all", encoding="utf-8")

    assert find_registration(tmp_path) is None


def test_every_host_registration_is_found_and_its_env_is_read(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The probe has to see the registration it exists to check.

    find_registration used to parse each settings file as JSON and look for an
    ``mcpServers`` dict. That described the hosts that shipped before this
    module did and nothing else: codex files its servers in TOML under
    ``mcp_servers``, continue in a YAML list, opencode under ``mcp``, and zed
    under ``context_servers``. Each of those read as no registration, so the
    permission probe never ran for them. That probe is what install points at
    for the client names it cannot verify, including zed's capital-Z ``Zed``,
    so the one check that could have caught it was the check that was skipped.

    Each fixture is written through the host's own shape and format, and each
    home is isolated so a registration elsewhere on the machine cannot answer
    in its place. That matters: before the fix, a real ``~/.claude.json`` was
    returned for all four hosts, which is worse than finding nothing, because
    it verifies an unrelated file and reports the answer as ours.
    """

    from continuum.mcp import configfmt
    from continuum.mcp.install import HOST_PROFILES, SERVER_NAME, host_shape

    pytest.importorskip("yaml")
    pytest.importorskip("tomli_w")
    for host, profile in HOST_PROFILES.items():
        # A directory per host: each fixture is written into a project root
        # that the next host must not inherit, and _isolated_home cannot
        # recreate a home that already exists.
        root = tmp_path / host
        root.mkdir()
        _isolated_home(monkeypatch, root)
        shape = host_shape(host)
        spec = configfmt.ServerSpec(
            SERVER_NAME,
            (str(SCRIPTS_DIR / "continuum-mcp"), "--db", str(root / "continuum.db")),
            {"CONTINUUM_MCP_MUTATING_CLIENTS": profile["mutating_clients"]},
        )
        data: dict[str, Any] = {}
        shape.put(data, "project", root, spec)

        settings = root / profile["project_settings"]
        settings.parent.mkdir(parents=True, exist_ok=True)
        if profile["format"] == "yaml":
            import yaml

            settings.write_text(yaml.safe_dump(data), encoding="utf-8")
        elif profile["format"] == "toml":
            import tomli_w

            settings.write_text(tomli_w.dumps(data), encoding="utf-8")
        else:
            settings.write_text(json.dumps(data, indent=2), encoding="utf-8")

        registration = doctor.find_registration(root)
        assert registration is not None, f"{host} registration was not found at all"
        assert registration["settings_path"] == settings
        # claude-code and gemini both register into .mcp.json, so a file that
        # one host wrote is equally the other's and which name comes back
        # depends on iteration order, not on the file. Every other host names
        # a file of its own, and for those the name has to be the right one.
        claimants = [
            name
            for name, other in HOST_PROFILES.items()
            if other["project_settings"] == profile["project_settings"]
        ]
        assert registration["host"] in claimants, (
            f"{host} registration resolved to {registration['host']} at "
            f"{registration['settings_path']}, which no profile names"
        )
        if len(claimants) == 1:
            assert registration["host"] == host, (
                f"{host} registration resolved to {registration['host']} at "
                f"{registration['settings_path']}"
            )
        assert registration["baked_client"] == profile["mutating_clients"]
        assert (
            doctor._registration_env(registration).get("CONTINUUM_MCP_MUTATING_CLIENTS")
            == profile["mutating_clients"]
        ), f"{host} env was read through the wrong key"

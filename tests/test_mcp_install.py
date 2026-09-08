"""``continuum mcp install`` — registration resolved on the machine that spawns it (issue #834).

The committed ``.mcp.json`` names the server by bare command, which a Windows host
cannot resolve from a venv it did not activate (#699), and no committed file can
carry one path that is right on every machine. The installer is the moving part
that makes a static file unnecessary: it resolves the entry point here, proves it
serves the protocol with a real handshake, and bakes the resolved path into the
host's config.

What is pinned here:

1. the written shape matches what ``claude mcp add`` itself writes (verified against
   the host), in every scope, with unrelated keys preserved;
2. idempotency: re-running converges (``present``), a moved venv repoints
   (``updated``) rather than duplicating;
3. the handshake gate: a server that does not answer ``initialize`` is refused
   *before* any config is written, and the server's own stderr surfaces — the
   diagnosis the host would otherwise swallow;
4. removal drops only the ``continuum-mcp`` key.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from continuum.cli import ExitCode, main
from continuum.mcp.install import (
    MCP_PROFILES,
    SERVER_KEY,
    build_registration,
    install_server,
    probe_server,
    remove_server,
    resolve_server_command,
)


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _project_key() -> str:
    """The key Claude Code files a local-scope registration under: cwd, posix-spelled."""
    return Path.cwd().resolve().as_posix()


def _registered(config: Path, scope: str) -> dict[str, Any]:
    data = json.loads(config.read_text(encoding="utf-8"))
    if scope == "local":
        return data["projects"][_project_key()]["mcpServers"][SERVER_KEY]
    return data["mcpServers"][SERVER_KEY]


def test_install_writes_the_shape_the_host_writes(tmp_path: Path) -> None:
    """Local scope lands where ``claude mcp add --scope local`` puts it: the user
    config's ``projects[cwd].mcpServers``, cwd in forward slashes, entry shaped
    ``{type, command, args, env}``. A shape the host did not write is a shape it
    may silently ignore."""
    config = tmp_path / "claude.json"
    code, out, err = run("--json", "mcp", "install", "--config", str(config))
    assert code == ExitCode.OK, err
    entry = _registered(config, "local")
    assert entry["type"] == "stdio"
    assert entry["env"] == {"CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code"}
    # The default database is the same relative path the committed .mcp.json
    # names, resolved by the host per project — not an absolute path that would
    # point every project at one database.
    assert entry["args"][-2:] == ["--db", "continuum.db"]
    payload = json.loads(out)
    assert payload["status"] == "installed"
    assert payload["verified"]["name"] == SERVER_KEY


def test_install_user_scope_writes_top_level(tmp_path: Path) -> None:
    """User scope registers the server for every project: top-level
    ``mcpServers`` in the same config file."""
    config = tmp_path / "claude.json"
    code, _, err = run("--json", "mcp", "install", "--scope", "user", "--config", str(config))
    assert code == ExitCode.OK, err
    entry = _registered(config, "user")
    assert entry["type"] == "stdio"
    # User scope must not fabricate a per-project entry on its way past.
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "projects" not in data


def test_install_project_scope_writes_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project scope targets the ``.mcp.json`` at the project root — the file the
    repo already ships — and the report says it is machine-specific, because a
    baked absolute path in a committed file is exactly the trap #699 fell into."""
    monkeypatch.chdir(tmp_path)
    code, out, err = run("--json", "mcp", "install", "--scope", "project")
    assert code == ExitCode.OK, err
    entry = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        SERVER_KEY
    ]
    assert entry["type"] == "stdio"
    assert "specific to this machine" in out


def test_reinstall_converges_and_repoints(tmp_path: Path) -> None:
    """Three installs leave one entry: the second is ``present`` (an upgrade that
    rewrote nothing), and a changed database repoints to ``updated`` — the
    moved-venv case, repointed rather than duplicated (#484's lesson, applied to
    a keyed store where it is simpler: the name *is* the identity)."""
    config = tmp_path / "claude.json"
    code, out, err = run("--json", "mcp", "install", "--config", str(config))
    assert code == ExitCode.OK, err
    assert json.loads(out)["status"] == "installed"

    code, out, err = run("--json", "mcp", "install", "--config", str(config))
    assert code == ExitCode.OK, err
    assert json.loads(out)["status"] == "present"

    code, out, err = run("--json", "mcp", "install", "--db", "other.db", "--config", str(config))
    assert code == ExitCode.OK, err
    assert json.loads(out)["status"] == "updated"
    assert _registered(config, "local")["args"][-1] == "other.db"


def test_install_preserves_unrelated_keys(tmp_path: Path) -> None:
    """The installer edits a file the host owns, in a section other tools share.
    Another server in the same ``mcpServers``, another project in ``projects``,
    and a top-level key must all survive byte-for-byte in value."""
    other_project = "C:/somewhere/else" if os.name == "nt" else "/somewhere/else"
    preexisting = {
        "numStartups": 42,
        "mcpServers": {"someone-elses-server": {"type": "stdio", "command": "other"}},
        "projects": {other_project: {"mcpServers": {"another": {"command": "x"}}}},
    }
    config = tmp_path / "claude.json"
    config.write_text(json.dumps(preexisting), encoding="utf-8")

    code, _, err = run("--json", "mcp", "install", "--scope", "user", "--config", str(config))
    assert code == ExitCode.OK, err
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["numStartups"] == 42
    assert data["mcpServers"]["someone-elses-server"]["command"] == "other"
    assert data["projects"][other_project]["mcpServers"]["another"]["command"] == "x"
    assert SERVER_KEY in data["mcpServers"]


def test_failed_probe_leaves_the_host_config_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handshake gate's whole point: a server that does not answer is never
    registered. #697 shipped because an entry point existed where its dependency
    did not; here that state fails the probe and the config file is not created."""
    from continuum.mcp import install as install_module

    def refuses(command: Any, **_: Any) -> dict[str, Any]:
        raise install_module.ProbeError("simulated: mcp extra not installed")

    monkeypatch.setattr(install_module, "probe_server", refuses)
    config = tmp_path / "claude.json"
    code, _, err = run("--json", "mcp", "install", "--config", str(config))
    assert code == ExitCode.ERROR
    assert "refusing to register" in err
    assert not config.exists()


def test_probe_surfaces_the_servers_own_stderr() -> None:
    """The host never shows a stdio server's stderr, so the probe has to: a child
    that exits with a remediation line on stderr must deliver that line to the
    operator, not a generic startup failure."""
    from continuum.mcp.install import ProbeError

    with pytest.raises(ProbeError) as excinfo:
        probe_server(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('install continuum-agent[mcp]\\n'); sys.exit(1)",
            ]
        )
    assert "continuum-agent[mcp]" in str(excinfo.value)


def test_probe_rejects_non_json_stdout() -> None:
    """stdout belongs to the protocol; a server that prints anything else there
    is unregistrable no matter how healthy it looks."""
    from continuum.mcp.install import ProbeError

    with pytest.raises(ProbeError):
        probe_server([sys.executable, "-c", "print('hello from a chatty server')"])


def test_probe_rejects_a_foreign_server() -> None:
    """A command that answers the handshake as some other server would register
    the wrong tool; the probe checks the name it was promised."""
    from continuum.mcp.install import ProbeError

    responder = (
        "import json,sys;"
        "print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'serverInfo':"
        "{'name':'someone-elses-server','version':'1'}}}))"
    )
    with pytest.raises(ProbeError, match="different server"):
        probe_server([sys.executable, "-c", responder])


def test_probe_accepts_the_real_server() -> None:
    """The interpreter fallback form — what a fresh clone without an activated
    venv resolves to on Windows — passes the probe, answering as continuum-mcp."""
    server = probe_server([sys.executable, "-u", "-m", "continuum.mcp"])
    assert server["name"] == SERVER_KEY


def test_install_refuses_to_clobber_invalid_json(tmp_path: Path) -> None:
    """A config someone hand-edited into invalid JSON is a statement of intent;
    the installer refuses to touch it rather than overwrite it (the
    ``clienthooks._install_hook`` contract, same situation)."""
    config = tmp_path / "claude.json"
    config.write_text("{not json", encoding="utf-8")
    code, _, err = run("--json", "mcp", "install", "--config", str(config))
    assert code == ExitCode.ERROR
    assert config.read_text(encoding="utf-8") == "{not json"


def test_remove_drops_only_our_key(tmp_path: Path) -> None:
    """Removal is scoped to ``continuum-mcp``: another server in the same map,
    and the project entry around it, survive — the mirror of install's
    preservation, and the same discipline ``clienthooks._remove_hooks`` keeps."""
    other_project = "C:/somewhere/else" if os.name == "nt" else "/somewhere/else"
    profile = MCP_PROFILES["claude-code"]
    config = tmp_path / "claude.json"
    registration = build_registration([r"C:\venv\continuum-mcp.exe"], client="claude-code")
    install_server(config, registration, scope="local", project_dir=Path.cwd(), profile=profile)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["mcpServers"] = {"someone-elses-server": {"command": "other"}}
    data["projects"][other_project] = {"allowedTools": ["Bash"]}
    config.write_text(json.dumps(data), encoding="utf-8")

    assert remove_server(config, scope="local", project_dir=Path.cwd(), profile=profile) is True
    data = json.loads(config.read_text(encoding="utf-8"))
    assert SERVER_KEY not in data["projects"][_project_key()]["mcpServers"]
    assert data["projects"][_project_key()]["mcpServers"] == {}
    assert data["mcpServers"]["someone-elses-server"]["command"] == "other"
    assert data["projects"][other_project]["allowedTools"] == ["Bash"]

    # Nothing left to remove: quiet no-op, not an error (an operator uninstalling
    # twice must not be told the second attempt failed).
    assert remove_server(config, scope="local", project_dir=Path.cwd(), profile=profile) is False


def test_remove_via_cli_reports_the_same_file_install_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #580 lesson: uninstall must resolve the same file install wrote, or
    install is a one-way door. Here both sides take ``--config``; remove must
    also work without it (the project-scope default is the cwd's .mcp.json)."""
    monkeypatch.chdir(tmp_path)
    code, _, err = run("--json", "mcp", "install", "--scope", "project")
    assert code == ExitCode.OK, err
    code, out, err = run("--json", "mcp", "remove", "--scope", "project")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert payload["removed"] is True
    # Relative, like the hooks installer's settings path: resolved against the
    # cwd the operator ran from.
    assert Path(payload["config"]) == Path(".mcp.json")
    assert (tmp_path / ".mcp.json").exists()
    assert (
        SERVER_KEY
        not in json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    )


def test_registration_forms() -> None:
    """Both resolved command shapes produce spawnable entries: an executable
    carries only the database flag; the interpreter form keeps its own argv
    (module flags before the database flag, in spawn order)."""
    exe = build_registration([r"C:\venv\Scripts\continuum-mcp.exe"])
    assert exe == {
        "type": "stdio",
        "command": r"C:\venv\Scripts\continuum-mcp.exe",
        "args": ["--db", "continuum.db"],
        "env": {"CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code"},
    }
    module = build_registration(
        [sys.executable, "-u", "-m", "continuum.mcp"], db="state/ledger.db", client="gemini"
    )
    assert module["command"] == sys.executable
    assert module["args"] == ["-u", "-m", "continuum.mcp", "--db", "state/ledger.db"]
    assert module["env"]["CONTINUUM_MCP_MUTATING_CLIENTS"] == "gemini"


def test_resolution_prefers_the_executable_then_the_sibling_then_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three-step resolution, most host-friendly first: an executable on PATH
    needs no environment; failing that, one beside the running ``continuum``
    (the unactivated-venv case); failing that, the interpreter form that needs
    no PATH at all — the only form that works for a fresh clone on Windows."""
    fake_exe = tmp_path / ("continuum-mcp.exe" if os.name == "nt" else "continuum-mcp")
    monkeypatch.setattr(
        "continuum.mcp.install.shutil.which",
        lambda name: str(fake_exe) if name == SERVER_KEY else None,
    )
    assert resolve_server_command() == [str(fake_exe)]

    running = tmp_path / ("continuum.exe" if os.name == "nt" else "continuum")
    running.write_text("", encoding="utf-8")
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "continuum.mcp.install.shutil.which",
        lambda name: str(running) if name == "continuum" else None,
    )
    assert resolve_server_command() == [str(fake_exe)]

    monkeypatch.setattr("continuum.mcp.install.shutil.which", lambda name: None)
    assert resolve_server_command() == [sys.executable, "-u", "-m", "continuum.mcp"]

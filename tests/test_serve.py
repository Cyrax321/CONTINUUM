"""The CONTINUUM sidecar wire protocol.

The property under test: the same operations the MCP server exposes are
reachable over a tiny newline-delimited JSON protocol from any process, with
fail-closed authentication and a real subprocess path that an external client
would use.
"""

from __future__ import annotations

import io
import json

import pytest

from continuum.serve import (
    MethodNotFound,
    NotAuthorized,
    SidecarAuth,
    SidecarServer,
    list_methods,
    serve_subprocess,
)


def make_server() -> SidecarServer:
    return SidecarServer(database=":memory:")


# --- methods ----------------------------------------------------------------


def test_list_methods_covers_the_surface() -> None:
    methods = list_methods()
    assert "record_progress" in methods
    assert "resume" in methods
    assert "intercept_action" in methods
    assert "reconcile_action" in methods
    assert len(methods) == 10


def test_record_progress_creates_the_run() -> None:
    srv = make_server()
    out = srv.dispatch(
        "record_progress", {"run_id": "r1", "completed": 3, "total": 10, "goal": "g"}
    )
    assert out["completed"] == 3
    assert out["total"] == 10


def test_record_progress_rejects_over_total() -> None:
    from continuum.serve.server import BadParams

    srv = make_server()
    with pytest.raises(BadParams, match="exceeds total"):
        srv.dispatch("record_progress", {"run_id": "r1", "completed": 5, "total": 4})


def test_checkpoint_and_resume_round_trip() -> None:
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "total": 10, "goal": "g"})
    cp = srv.dispatch("checkpoint", {"run_id": "r1"})
    assert cp["checkpoint_id"]
    decision = srv.dispatch("resume", {"run_id": "r1"})
    # A self-certified (agent-reported) run is not auto-resumable until a human
    # confirms it, so the mode is request_human here; the round trip itself is
    # what this test exercises.
    assert decision["mode"] in {"resume", "repair_and_resume", "request_human"}
    assert decision["progress"]["completed"] == 1


def test_checkpointing_with_env_makes_drift_block_resume() -> None:
    """The sidecar must not disagree with the MCP surface about drift.

    Pinning an environment at checkpoint has to declare it as a dependency, or
    the validator has nothing to invalidate and a moved resource still reports
    safe. Confirming the run first clears the self-report review so the
    environment check is what decides the verdict.
    """
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "total": 10, "goal": "g"})
    srv.dispatch("checkpoint", {"run_id": "r1", "env": {"dataset": "v3"}})
    srv.dispatch("confirm", {"run_id": "r1"})

    clean = srv.dispatch("validate", {"run_id": "r1", "env": {"dataset": "v3"}})
    assert clean["safe"] is True

    drifted = srv.dispatch("validate", {"run_id": "r1", "env": {"dataset": "v4"}})
    assert drifted["safe"] is False
    assert any(
        c["component"] == "external_dependency" and c["status"] == "conflicted"
        for c in drifted["components"]
    )


def test_env_accepts_the_name_equals_version_list_shape() -> None:
    """Both accepted ``env`` shapes must declare dependencies identically."""
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "goal": "g"})
    srv.dispatch("checkpoint", {"run_id": "r1", "env": ["dataset=v3"]})
    srv.dispatch("confirm", {"run_id": "r1"})

    drifted = srv.dispatch("validate", {"run_id": "r1", "env": ["dataset=v4"]})
    assert drifted["safe"] is False


def test_list_actions_marks_an_interrupted_row_unresolved() -> None:
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "goal": "g"})
    done = srv.dispatch("intercept_action", {"run_id": "r1", "action_type": "a.do", "key": "k1"})
    srv.dispatch("complete_action", {"run_id": "r1", "action_key": done["action_key"]})
    srv.dispatch("intercept_action", {"run_id": "r1", "action_type": "b.do", "key": "k2"})

    rows = {a["action_type"]: a for a in srv.dispatch("list_actions", {"run_id": "r1"})["actions"]}
    assert rows["a.do"]["outcome_unresolved"] is False
    assert rows["b.do"]["outcome_unresolved"] is True


def test_intercept_then_complete_action() -> None:
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "goal": "g"})
    claim = srv.dispatch("intercept_action", {"run_id": "r1", "action_type": "x.do", "key": "k1"})
    assert claim["proceed"] is True
    done = srv.dispatch("complete_action", {"run_id": "r1", "action_key": claim["action_key"]})
    assert done["status"] == "completed"
    listed = srv.dispatch("list_actions", {"run_id": "r1"})
    assert listed["actions"][0]["status"] == "completed"


def test_unknown_method_is_not_found() -> None:
    srv = make_server()
    with pytest.raises(MethodNotFound):
        srv.dispatch("nope", {})


def test_stdio_loop_reads_jsonl_and_answers() -> None:
    srv = make_server()
    requests = (
        "\n".join(
            [
                json_line(0, "record_progress", {"run_id": "r1", "completed": 2, "goal": "g"}),
                json_line(1, "resume", {"run_id": "r1"}),
                json_line(2, "bogus", {}),
            ]
        )
        + "\n"
    )
    out = io.StringIO()
    srv.serve_stdio(io.StringIO(requests), out)
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3
    assert json.loads(lines[0])["result"]["completed"] == 2
    assert json.loads(lines[1])["result"]["mode"]
    assert json.loads(lines[2])["error"]["type"] == "method_not_found"


# --- authentication (fail-closed) ------------------------------------------


def test_auth_refuses_without_token_when_required(monkeypatch) -> None:
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "secret")
    srv = make_server()
    with pytest.raises(NotAuthorized):
        srv.dispatch("record_progress", {"run_id": "r1", "completed": 1})


def test_auth_allows_the_correct_token(monkeypatch) -> None:
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "secret")
    srv = make_server()
    out = srv.dispatch(
        "record_progress",
        {"run_id": "r1", "completed": 1, "goal": "g", "auth_token": "secret"},
    )
    assert out["completed"] == 1


def test_auth_disabled_by_default() -> None:
    auth = SidecarAuth()
    assert auth.disabled
    auth.verify(None)  # must not raise


# --- real subprocess path (what an external client uses) --------------------


def test_serve_subprocess_end_to_end(tmp_path) -> None:
    db = tmp_path / "run.db"
    client = serve_subprocess(db=str(db))
    try:
        out = client.request("record_progress", run_id="r1", completed=4, total=10, goal="g")
        assert out["completed"] == 4
        cp = client.request("checkpoint", run_id="r1")
        assert cp["checkpoint_id"]
        decision = client.request("resume", run_id="r1")
        assert decision["mode"]
        # a fresh action is allowed, then completed
        claim = client.request("intercept_action", run_id="r1", action_type="x.do", key="k1")
        assert claim["proceed"] is True
        done = client.request("complete_action", run_id="r1", action_key=claim["action_key"])
        assert done["status"] == "completed"
    finally:
        client.terminate()


def json_line(rid: int, method: str, params: dict) -> str:
    import json

    return json.dumps({"id": rid, "method": method, "params": params})

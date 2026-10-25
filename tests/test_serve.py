"""The CONTINUUM sidecar wire protocol.

The property under test: the same operations the MCP server exposes are
reachable over a tiny newline-delimited JSON protocol from any process, with
fail-closed authentication and a real subprocess path that an external client
would use.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.serve import (
    MethodNotFound,
    NotAuthorized,
    SidecarAuth,
    SidecarServer,
    list_methods,
    serve_subprocess,
)
from continuum.serve.server import MUTATING, BadParams
from continuum.storage import ConcurrentWriteError


def make_server() -> SidecarServer:
    return SidecarServer(database=":memory:")


# --- methods ----------------------------------------------------------------


def test_list_methods_covers_the_surface() -> None:
    methods = list_methods()
    assert "record_progress" in methods
    assert "resume" in methods
    assert "intercept_action" in methods
    assert "reconcile_action" in methods
    assert "compensate_action" in methods
    assert len(methods) == 11


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


def test_record_progress_refuses_an_update_the_fold_rejects_and_records_nothing() -> None:
    """The write path must reject what the read path rejects, before committing
    (issue #1221). The pre-write guards see only this call's own arguments, so
    they are blind to the ``total`` already on record -- the ordinary case, since
    an agent reports ``total`` once and then sends ``completed``/``failed``
    updates. Only the fold sees the accumulated figures."""
    from continuum.events import EventType
    from continuum.serve.server import BadParams
    from continuum.state.semantic import project

    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 5, "goal": "g"})
    # completed(4) + failed(4) = 8 exceeds the recorded total of 5, but the
    # payload itself carries no total, so the same-payload guard cannot see it.
    with pytest.raises(BadParams, match="would leave run 'r1' unprojectable"):
        srv.dispatch("record_progress", {"run_id": "r1", "completed": 4, "failed": 4})

    events = srv.storage.read_events("r1")
    updates = [e for e in events if e.type is EventType.TASK_UPDATED]
    assert len(updates) == 1, "the refused update must not be recorded"
    assert updates[0].payload["total"] == 5
    # The run is still usable: every projecting surface still resolves.
    state = project("r1", events)
    assert state.progress.total == 5
    assert state.progress.completed == 0


def test_record_progress_reports_the_accepted_figures_from_the_new_state() -> None:
    """A legal update lands and the response carries the folded figures,
    including the pending count derived from the recorded total."""
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "total": 5, "goal": "g"})
    out = srv.dispatch("record_progress", {"run_id": "r1", "completed": 3, "failed": 1})
    assert (out["completed"], out["failed"], out["total"], out["pending"]) == (3, 1, 5, 1)
    assert out["source_sequence"] == 3


def test_record_progress_retries_past_a_won_concurrency_race() -> None:
    """``expected_sequence`` spans the validate/commit gap, and a second writer
    can advance the run between them (#1221). Losing that race says nothing
    about whether this update is valid, so it is re-read, re-validated against
    the new head and committed rather than failed."""
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 5, "goal": "g"})
    real_append = srv.storage.append_event
    lost = {"once": False}

    def racing_append(*args: object, **kwargs: object) -> object:
        if not lost["once"]:
            lost["once"] = True
            raise ConcurrentWriteError("simulated")
        return real_append(*args, **kwargs)

    srv.storage.append_event = racing_append
    out = srv.dispatch("record_progress", {"run_id": "r1", "completed": 2})
    assert out["completed"] == 2
    assert out["total"] == 5
    # The retried write landed exactly once.
    assert srv.storage.last_sequence("r1") == out["source_sequence"]


def test_record_progress_refuses_when_it_loses_every_concurrency_race() -> None:
    """A run that loses the race on every attempt answers with a bad_params
    naming the competing owner and records nothing, rather than spinning or
    leaving a half-applied update (#1221)."""
    from continuum.events import EventType

    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 0, "total": 5, "goal": "g"})
    recorded_before = [e for e in srv.storage.read_events("r1") if e.type is EventType.TASK_UPDATED]

    def always_racing(*args: object, **kwargs: object) -> object:
        raise ConcurrentWriteError("simulated")

    srv.storage.append_event = always_racing
    with pytest.raises(BadParams, match="is being written concurrently"):
        srv.dispatch("record_progress", {"run_id": "r1", "completed": 2})
    recorded_after = [e for e in srv.storage.read_events("r1") if e.type is EventType.TASK_UPDATED]
    assert recorded_after == recorded_before, "the losing update must not be recorded"


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


# --- resume mirrors the MCP surface (issue #91) ------------------------------


def test_resume_returns_the_run_goal() -> None:
    """A resumed client must learn what the task was, not just how far it got.

    Without this the sidecar is the one boundary that still needs an external
    task file to answer "what was I doing", which is the overhead the goal was
    added to continuum_resume to remove.
    """
    srv = make_server()
    srv.dispatch(
        "record_progress",
        {"run_id": "r1", "completed": 3, "total": 10, "goal": "migrate the billing module"},
    )
    srv.dispatch("checkpoint", {"run_id": "r1"})

    decision = srv.dispatch("resume", {"run_id": "r1"})
    assert decision["goal"] == "migrate the billing module"


def test_resume_without_run_id_targets_the_active_run() -> None:
    """An interrupted session has no id to send, so omitting it must work.

    This is the whole point of the capability: a fresh process that lost its
    memory of the run can still ask what to continue.
    """
    srv = make_server()
    srv.dispatch(
        "record_progress",
        {"run_id": "r1", "completed": 3, "total": 10, "goal": "migrate the billing module"},
    )
    srv.dispatch("checkpoint", {"run_id": "r1"})

    decision = srv.dispatch("resume", {})
    assert decision["run_id"] == "r1"
    assert decision["goal"] == "migrate the billing module"
    assert decision["progress"]["completed"] == 3


def test_resume_without_run_id_reports_no_active_run() -> None:
    """Nothing to resume is a verdict, not a protocol error.

    Reported as a mode so a client can branch on ``mode`` alone and get the
    same answer from the sidecar as from continuum_resume.
    """
    srv = make_server()
    decision = srv.dispatch("resume", {})
    assert decision["mode"] == "no_active_run"
    assert decision["safe"] is False
    assert decision["run_id"] is None


def test_resume_still_requires_review_for_a_self_reported_run() -> None:
    """Returning the goal must not confirm it.

    The goal is a self-report from a remote caller, so surfacing it is
    read-only and the run stays behind the human-review gate.
    """
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "total": 10, "goal": "g"})
    srv.dispatch("checkpoint", {"run_id": "r1"})

    assert srv.dispatch("resume", {"run_id": "r1"})["mode"] == "request_human"


async def test_resume_payload_covers_the_mcp_resume_surface() -> None:
    """Every field continuum_resume returns must also come back from the sidecar.

    Compared against the live MCP payload rather than a hardcoded key list,
    because a field added there and forgotten here is exactly how ``goal`` went
    missing. Skipped without the ``mcp`` extra, which ``continuum serve`` is
    designed not to need.
    """
    pytest.importorskip("mcp")
    from continuum.mcp.authz import AuthorizationPolicy
    from continuum.mcp.server import build_server
    from continuum.storage.sqlite import SQLiteStorage
    from tests.mcp_helpers import fake_context

    caller = "pytest-client"
    server, ctx = build_server(
        storage=SQLiteStorage(":memory:"), policy=AuthorizationPolicy([caller])
    )
    try:

        async def call(name: str, **arguments: object) -> dict:
            result = await server.call_tool(name, arguments, context=fake_context(caller))
            return dict(json.loads(result.content[0].text))

        await call("continuum_record_progress", run_id="r1", completed=3, total=10, goal="g")
        await call("continuum_checkpoint", run_id="r1")
        expected = await call("continuum_resume", run_id="r1")
    finally:
        ctx.close()

    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 3, "total": 10, "goal": "g"})
    srv.dispatch("checkpoint", {"run_id": "r1"})
    actual = srv.dispatch("resume", {"run_id": "r1"})

    missing = sorted(set(expected) - set(actual))
    assert not missing, f"sidecar resume omits MCP fields: {missing}"

    missing_progress = sorted(set(expected["progress"]) - set(actual["progress"]))
    assert not missing_progress, f"sidecar progress omits MCP fields: {missing_progress}"


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


def test_intercept_complete_then_compensate_action() -> None:
    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "goal": "g"})
    claim = srv.dispatch("intercept_action", {"run_id": "r1", "action_type": "x.do", "key": "k1"})
    assert claim["proceed"] is True
    done = srv.dispatch("complete_action", {"run_id": "r1", "action_key": claim["action_key"]})
    assert done["status"] == "completed"
    compensated = srv.dispatch(
        "compensate_action",
        {"run_id": "r1", "action_key": claim["action_key"], "note": "refunded", "by": "action_2"},
    )
    assert compensated["status"] == "compensated"
    assert compensated["compensated_by"] == ["action_2"]
    listed = srv.dispatch("list_actions", {"run_id": "r1"})
    assert listed["actions"][0]["status"] == "compensated"


def test_complete_and_reconcile_forward_consumed_inputs() -> None:
    """Sidecar complete/reconcile record caller-supplied consumed_inputs (#558)."""
    from continuum.actions.ledger import fold_action_events

    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "goal": "g"})
    claim = srv.dispatch("intercept_action", {"run_id": "r1", "action_type": "x.do", "key": "k1"})
    ci = {
        "checkpoint_seq": 2,
        "event_positions": [1],
        "component_ids": ["d1"],
        "action_ids": ["a1"],
    }
    done = srv.dispatch(
        "complete_action",
        {"run_id": "r1", "action_key": claim["action_key"], "consumed_inputs": ci},
    )
    assert done["status"] == "completed"
    folded = fold_action_events(srv.storage.read_events("r1"))
    assert folded[claim["action_key"]].consumed_inputs.checkpoint_seq == 2
    assert folded[claim["action_key"]].consumed_inputs.action_ids == ["a1"]
    srv.dispatch(
        "reconcile_action",
        {
            "run_id": "r1",
            "action_key": claim["action_key"],
            "occurred": True,
            "consumed_inputs": {"checkpoint_seq": 3},
        },
    )
    refolded = fold_action_events(srv.storage.read_events("r1"))
    assert refolded[claim["action_key"]].consumed_inputs.checkpoint_seq == 3


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


def test_stdio_resume_needs_no_params_at_all() -> None:
    """The wire shape a restarted foreign client actually sends (issue #91).

    Such a client knows the method and nothing else, so ``params`` may be absent
    entirely rather than merely lacking ``run_id``. It must still come back with
    the active run and its goal instead of a ``bad_params`` error.
    """
    srv = make_server()
    requests = (
        "\n".join(
            [
                json_line(
                    0,
                    "record_progress",
                    {"run_id": "r1", "completed": 3, "total": 10, "goal": "migrate billing"},
                ),
                json_line(1, "checkpoint", {"run_id": "r1"}),
                '{"id": 2, "method": "resume"}',
            ]
        )
        + "\n"
    )
    out = io.StringIO()
    srv.serve_stdio(io.StringIO(requests), out)

    last = json.loads([line for line in out.getvalue().splitlines() if line.strip()][-1])
    assert "error" not in last, last
    assert last["result"]["run_id"] == "r1"
    assert last["result"]["goal"] == "migrate billing"


@pytest.mark.parametrize("body", ("[]", "null", "5", '"resume"', "true", '["resume"]'))
def test_stdio_answers_a_non_object_request_and_keeps_serving(body: str) -> None:
    """A request that parses but is not an object is a client error (issue #582).

    ``rid = req.get("id")`` sat one line above the guard whose own comment reads
    "report, never crash the loop", so a line like ``[]`` raised
    ``AttributeError`` where nothing was catching and the process exited 1.
    Because stdio is a long-lived session, one malformed line from any client
    took the durability plane down and every later request on that connection
    with it. It must be answered like a line that is not JSON at all --
    ``bad_request``, with a null id, since a non-object request has nowhere to
    put one -- and the request behind it must still be served.
    """
    srv = make_server()
    good = json_line(1, "record_progress", {"run_id": "r1", "completed": 2, "goal": "g"})
    out = io.StringIO()

    assert srv.serve_stdio(io.StringIO(f"{body}\n{good}\n"), out) == 0

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2, lines
    assert lines[0] == {
        "id": None,
        "error": {"type": "bad_request", "message": "body must be a JSON object"},
    }
    assert lines[1]["id"] == 1
    assert lines[1]["result"]["completed"] == 2


def test_stdio_still_answers_a_line_that_is_not_json_at_all() -> None:
    """The framing answer #582 reuses, pinned so the shared arm cannot change it.

    A line that no parser can read keeps its own message (the decoder's), which
    is what tells the two cases apart on the wire: same ``bad_request`` type and
    null id, different text.
    """
    srv = make_server()
    out = io.StringIO()

    assert srv.serve_stdio(io.StringIO("not json at all\n"), out) == 0

    answer = json.loads(out.getvalue().strip())
    assert answer["id"] is None
    assert answer["error"]["type"] == "bad_request"
    assert answer["error"]["message"] != "body must be a JSON object"
    assert "Expecting value" in answer["error"]["message"]


def test_stdio_survives_a_run_of_unreadable_requests() -> None:
    """The loop is a session, so the failure has to be survivable in series.

    One answer per bad line, in order, and a working request at the end still
    lands: the whole point of #582 is that the connection is not lost.
    """
    srv = make_server()
    requests = (
        "\n".join(
            [
                "[]",
                "not json",
                "null",
                json_line(9, "record_progress", {"run_id": "r1", "completed": 1, "goal": "g"}),
            ]
        )
        + "\n"
    )
    out = io.StringIO()

    assert srv.serve_stdio(io.StringIO(requests), out) == 0

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [line["id"] for line in lines] == [None, None, None, 9]
    assert [line["error"]["type"] for line in lines[:3]] == ["bad_request"] * 3
    assert lines[3]["result"]["completed"] == 1


# --- authentication (fail-closed) ------------------------------------------


def test_auth_refuses_without_token_when_required(monkeypatch) -> None:
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "actual-token-value")
    srv = make_server()
    with pytest.raises(NotAuthorized) as exc_info:
        srv.dispatch("record_progress", {"run_id": "r1", "completed": 1})
    assert "CONTINUUM_SERVE_TOKEN" in str(exc_info.value)
    assert "auth_token" in str(exc_info.value)
    assert "actual-token-value" not in str(exc_info.value)


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


@pytest.mark.parametrize("method", list_methods())
def test_every_method_requires_the_secret(monkeypatch, method: str) -> None:
    """The sidecar's real policy, pinned exhaustively (issue #95).

    The two token tests in this section both drive ``record_progress``, so
    between them they only proved the policy for one mutating method -- which
    left the suite consistent with a mutating-only policy that the sidecar does
    not implement. Driving every entry in ``list_methods()`` closes that, and
    keeps a method added later from landing unauthenticated by default.

    ``dispatch`` verifies before it routes, so the missing ``run_id`` here
    cannot be what raises: ``BadParams`` would mean auth had already been
    skipped.
    """
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "secret")
    srv = make_server()

    with pytest.raises(NotAuthorized):
        srv.dispatch(method, {"run_id": "r1"})


def test_the_mutating_constant_does_not_govern_authentication(monkeypatch) -> None:
    """Regression for #95: the exported constant is metadata, not the policy.

    ``MUTATING`` omits the three read-only methods, and until this test the only
    trace of a mutating-only rule was an unreferenced ``_auth_check`` helper.
    Deleting the helper is not enough on its own -- someone reading the constant
    can reintroduce the same gate in ``dispatch``, and every other test in this
    file would still pass while ``resume``, ``validate`` and ``list_actions``
    silently opened up to unauthenticated callers, in precisely the deployments
    that bothered to set a secret. This is the test that goes red instead.
    """
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "secret")
    srv = make_server()

    read_only = [method for method in list_methods() if method not in MUTATING]
    assert read_only == ["list_actions", "resume", "validate"], (
        "the set of methods MUTATING leaves out has changed; "
        "confirm the auth policy still covers them"
    )

    for method in read_only:
        with pytest.raises(NotAuthorized):
            srv.dispatch(method, {"run_id": "r1"})


def test_a_read_only_method_succeeds_with_the_secret(monkeypatch) -> None:
    """Closing reads must gate them, not break them.

    Also shows what the gate is protecting: ``resume`` returns the goal string,
    and ``list_actions`` the arguments and results of real side effects. That is
    the argument for the sidecar being stricter than the MCP server -- anything
    that can reach this pipe can ask for it.
    """
    monkeypatch.setenv("CONTINUUM_SERVE_TOKEN", "secret")
    srv = make_server()
    srv.dispatch(
        "record_progress",
        {
            "run_id": "r1",
            "completed": 1,
            "total": 3,
            "goal": "migrate billing",
            "auth_token": "secret",
        },
    )

    out = srv.dispatch("resume", {"run_id": "r1", "auth_token": "secret"})

    assert out["goal"] == "migrate billing"
    assert out["progress"]["completed"] == 1


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


def test_malformed_consumed_inputs_is_bad_params_not_internal() -> None:
    """Ledger validation failures surface as parameter errors (#645 review)."""
    from continuum.serve.server import BadParams

    srv = make_server()
    srv.dispatch("record_progress", {"run_id": "r1", "completed": 1, "goal": "g"})
    claim = srv.dispatch("intercept_action", {"run_id": "r1", "action_type": "x.do", "key": "k1"})
    with pytest.raises(BadParams, match="greater than or equal"):
        srv.dispatch(
            "complete_action",
            {
                "run_id": "r1",
                "action_key": claim["action_key"],
                "consumed_inputs": {"checkpoint_seq": -1},
            },
        )
    with pytest.raises(BadParams, match="consumed_inputs"):
        srv.dispatch(
            "reconcile_action",
            {
                "run_id": "r1",
                "action_key": claim["action_key"],
                "occurred": True,
                "consumed_inputs": [1, 2],
            },
        )


def test_serve_ensure_run_recognises_archived_run_started_after_compaction(tmp_path: Path) -> None:
    """SidecarServer._ensure_run must not append duplicate RUN_STARTED after compaction (#1436)."""
    from continuum.events import EventType
    from continuum.models import Origin, Run
    from continuum.state.semantic import project
    from continuum.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(str(tmp_path / "serve_compacted_ensure.db"))
    storage.create_run(Run(run_id="run_sc", goal="deliver package"))
    storage.append_event(
        "run_sc",
        EventType.RUN_STARTED,
        {"goal": "deliver package", "constraints": ["fragile", "same-day"], "total": 5},
        source=Origin.HUMAN,
    )
    storage.append_event(
        "run_sc",
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "pack item"},
        source=Origin.HUMAN,
    )
    storage.compact_run("run_sc")

    srv = SidecarServer(storage=storage)
    srv._ensure_run("run_sc")

    events = storage.read_all_events("run_sc")
    run_started_events = [e for e in events if e.type is EventType.RUN_STARTED]
    assert len(run_started_events) == 1

    state = project("run_sc", events)
    assert state.goal.constraints == ["fragile", "same-day"]


def test_serve_checkpoint_projects_full_history_after_compaction(tmp_path: Path) -> None:
    """_h_checkpoint must fold the archive, not just the live tail.

    ``_ensure_run`` already accepts a compacted run (#1452), but the checkpoint
    handler projected the live tail alone, so a run whose ``RUN_STARTED`` had
    been archived raised "no goal" instead of checkpointing -- on exactly the
    long-running runs the endpoint exists for.
    """
    from continuum.events import EventType
    from continuum.models import Origin, Run
    from continuum.state.semantic import project
    from continuum.storage.sqlite import SQLiteStorage

    storage = SQLiteStorage(str(tmp_path / "serve_compacted_checkpoint.db"))
    storage.create_run(Run(run_id="run_sc", goal="deliver package"))
    storage.append_event(
        "run_sc",
        EventType.RUN_STARTED,
        {"goal": "deliver package", "constraints": ["fragile", "same-day"], "total": 5},
        source=Origin.HUMAN,
    )
    storage.append_event(
        "run_sc",
        EventType.WORK_ADDED,
        {"task_id": "w1", "description": "pack item"},
        source=Origin.HUMAN,
    )
    storage.compact_run("run_sc")
    assert not any(e.type is EventType.RUN_STARTED for e in storage.read_events("run_sc"))

    srv = SidecarServer(storage=storage)
    response = srv.dispatch("checkpoint", {"run_id": "run_sc", "reason": "post-compaction"})

    assert response["run_id"] == "run_sc"
    # The checkpoint was taken over a state that still sees the archived goal.
    state = project("run_sc", storage.read_all_events("run_sc"))
    assert state.goal is not None
    assert state.goal.constraints == ["fragile", "same-day"]


# --- the wire client (#1746) ------------------------------------------------


def test_sidecar_client_round_trips_one_request_over_in_memory_streams() -> None:
    """The client's outgoing frame is exactly what a real stdio server answers."""
    from continuum.serve import SidecarClient

    reply_wire = io.StringIO()
    frame = (
        json_line(0, "record_progress", {"run_id": "r1", "completed": 2, "total": 5, "goal": "g"})
        + "\n"
    )
    make_server().serve_stdio(io.StringIO(frame), reply_wire)

    out = io.StringIO()
    client = SidecarClient(io.StringIO(reply_wire.getvalue()), out)
    result = client.request("record_progress", run_id="r1", completed=2, total=5, goal="g")

    assert result["completed"] == 2
    assert result["pending"] == 3
    assert result["total"] == 5
    # The frame the client wrote is the frame the server just accepted, byte for byte.
    assert out.getvalue() == frame


def test_sidecar_client_raises_on_error_response() -> None:
    """An error frame is a :class:`SidecarClientError`, never a returned dict."""
    from continuum.serve import SidecarClient, SidecarClientError

    out = io.StringIO()
    inbox = io.StringIO(
        '{"id": 0, "error": {"type": "not_authorized", "message": "no token supplied"}}\n'
    )
    client = SidecarClient(inbox, out)
    with pytest.raises(SidecarClientError, match="not_authorized: no token supplied"):
        client.request("resume", run_id="r1")


def test_sidecar_client_raises_when_the_sidecar_closes_the_connection() -> None:
    """EOF mid-request is reported rather than returning ``None``."""
    from continuum.serve import SidecarClient, SidecarClientError

    client = SidecarClient(io.StringIO(""), io.StringIO())
    with pytest.raises(SidecarClientError, match="sidecar closed the connection"):
        client.request("resume", run_id="r1")


def test_sidecar_client_skips_blank_lines_and_other_ids() -> None:
    """Blank lines and frames for other ids are not answers, they are noise."""
    from continuum.serve import SidecarClient

    inbox = io.StringIO(
        '\n{"id": 99, "result": {"mode": "replay"}}\n{"id": 0, "result": {"mode": "resume"}}\n'
    )
    client = SidecarClient(inbox, io.StringIO())
    assert client.request("resume") == {"mode": "resume"}


def test_sidecar_client_request_id_advances_per_call() -> None:
    """Each call carries the next id, so interleaved replies stay separable."""
    from continuum.serve import SidecarClient

    out = io.StringIO()
    inbox = io.StringIO('{"id": 0, "result": {"ok": "a"}}\n{"id": 1, "result": {"ok": "b"}}\n')
    client = SidecarClient(inbox, out)

    assert client.request("record_progress") == {"ok": "a"}
    assert client.request("record_progress") == {"ok": "b"}

    sent = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [line["id"] for line in sent] == [0, 1]
    assert [line["method"] for line in sent] == ["record_progress", "record_progress"]


def test_sidecar_client_close_suppresses_errors_from_a_broken_out_stream() -> None:
    """``close`` never propagates: a server must still shut down cleanly."""
    from continuum.serve import SidecarClient

    class Exploding(io.StringIO):
        def close(self) -> None:
            raise OSError("already closed elsewhere")

    client = SidecarClient(io.StringIO(""), Exploding())
    client.close()  # must not raise


def test_subprocess_client_delegates_to_the_wrapped_client() -> None:
    """``SubprocessClient.__getattr__`` forwards to the wire client."""
    from continuum.serve import SidecarClient, SubprocessClient

    inbox = io.StringIO('{"id": 0, "result": {"mode": "resume"}}\n')
    inner = SidecarClient(inbox, io.StringIO())
    proc = SubprocessClient(_FakeProcess(), inner)

    assert proc.request("resume") == {"mode": "resume"}
    assert proc._next_id == 1  # the attribute lives on the wrapped client


def test_subprocess_client_terminate_kills_a_process_that_ignores_sigterm() -> None:
    """``terminate`` escalates to kill when the five-second wait expires."""
    from continuum.serve import SidecarClient, SubprocessClient
    from continuum.serve.server import SidecarServer  # noqa: F401  (import sanity)

    proc = _FakeProcess(hang=True)
    client = SubprocessClient(proc, SidecarClient(io.StringIO(""), io.StringIO()))
    client.terminate()

    assert proc.terminated
    assert proc.killed


def test_subprocess_client_terminate_waits_for_a_polite_exit() -> None:
    from continuum.serve import SidecarClient, SubprocessClient

    proc = _FakeProcess()
    client = SubprocessClient(proc, SidecarClient(io.StringIO(""), io.StringIO()))
    client.terminate()

    assert proc.terminated
    assert not proc.killed


class _FakeProcess:
    """The bit of ``subprocess.Popen`` ``SubprocessClient`` and
    ``serve_subprocess`` depend on, without a real process."""

    def __init__(self, hang: bool = False) -> None:
        self.terminated = False
        self.killed = False
        self.hang = hang

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        import subprocess as _sp

        if self.hang and timeout is not None:
            raise _sp.TimeoutExpired(cmd="continuum serve", timeout=timeout)
        return 0


def test_serve_subprocess_passes_the_token_through_the_environment(monkeypatch) -> None:
    """The token reaches the child as ``CONTINUUM_SERVE_TOKEN``."""
    import subprocess

    from continuum.serve import serve_subprocess

    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, stdin, stdout, env, text, bufsize) -> None:
            captured["env"] = env
            self.stdin = stdin
            self.stdout = stdout

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    serve_subprocess("ignored.db", token="sekret")

    assert captured["env"]["CONTINUUM_SERVE_TOKEN"] == "sekret"


def test_serve_subprocess_omits_the_token_when_none_was_given(monkeypatch) -> None:
    import subprocess

    from continuum.serve import serve_subprocess

    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, stdin, stdout, env, text, bufsize) -> None:
            captured["env"] = env
            self.stdin = stdin
            self.stdout = stdout

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    serve_subprocess("ignored.db")

    assert "CONTINUUM_SERVE_TOKEN" not in captured["env"]


def test_run_serve_over_stdio_returns_zero_and_closes_the_server(monkeypatch) -> None:
    """The stdio branch is the default transport; the server always closes."""
    import continuum.serve
    from continuum.serve import run_serve

    closed: list[bool] = []

    class FakeServer:
        def __init__(self, database=None) -> None:
            pass

        def serve_stdio(self, instream, outstream) -> int:
            return 0

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(continuum.serve, "SidecarServer", FakeServer)

    assert run_serve(db=None, transport="stdio") == 0
    assert closed == [True]


def test_run_serve_over_http_shuts_the_server_down(monkeypatch) -> None:
    """The http branch runs the loop, then shuts down and closes the server."""
    import continuum.serve
    from continuum.serve import run_serve

    events: list[str] = []

    class FakeHTTP:
        def __init__(self, server, port: int = 8765, bind: str = "127.0.0.1") -> None:
            events.append(f"init:{port}")
            self.port = port
            self.bind = bind

        def serve_forever(self) -> None:
            events.append("serve")

        def shutdown(self) -> None:
            events.append("shutdown")

    class FakeServer:
        def __init__(self, database=None) -> None:
            events.append(f"server:{database}")

        def serve_stdio(self, instream, outstream) -> int:  # pragma: no cover
            return 0

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(continuum.serve, "SidecarServer", FakeServer)
    monkeypatch.setattr("continuum.serve.server.SidecarHTTP", FakeHTTP)

    assert run_serve(db=None, transport="http", port=9100) == 0
    assert events == ["server:None", "init:9100", "serve", "shutdown", "close"]


def test_run_serve_http_survives_ctrl_c_and_still_shuts_down(monkeypatch, capsys) -> None:
    """``KeyboardInterrupt`` is the normal way a foreground server stops."""
    import continuum.serve
    from continuum.serve import run_serve

    events: list[str] = []

    class FakeHTTP:
        def __init__(self, server, port: int = 8765, bind: str = "127.0.0.1") -> None:
            events.append(f"init:{port}")
            self.port = port
            self.bind = bind

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            events.append("shutdown")

    class FakeServer:
        def __init__(self, database=None) -> None:
            events.append(f"server:{database}")

        def serve_stdio(self, instream, outstream) -> int:  # pragma: no cover
            return 0

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(continuum.serve, "SidecarServer", FakeServer)
    monkeypatch.setattr("continuum.serve.server.SidecarHTTP", FakeHTTP)

    assert run_serve(db=None, transport="http") == 0
    # The finally block is the whole point: shutdown then close must still run
    # after the interrupt, in that order.
    assert events == ["server:None", "init:8765", "shutdown", "close"]
    assert "CONTINUUM sidecar listening on http://127.0.0.1:8765" in capsys.readouterr().out


def test_cmd_serve_runs_over_stdio_and_closes_the_storage(tmp_path) -> None:
    """``cmd_serve`` hands the CLI's parsed args to ``run_serve`` and returns it."""
    from continuum.serve import cmd_serve
    from continuum.storage.sqlite import SQLiteStorage

    closed: list[bool] = []

    class TrackingStorage(SQLiteStorage):
        def close(self) -> None:
            closed.append(True)
            super().close()

    s = TrackingStorage(str(tmp_path / "cmd_serve2.db"))

    class Args:
        # run_serve builds a real SidecarServer from this, so None (or
        # CONTINUUM_DB) would open continuum.db in the working directory.
        db = str(tmp_path / "cmd_serve.db")
        transport = "stdio"
        port = 8765

    # run_serve's stdio branch defaults to sys.stdin/sys.stdout, which pytest
    # does not let it read; an empty stream is the "no requests" shape.
    import sys

    real_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        assert cmd_serve(Args, s, io.StringIO(), io.StringIO()) == 0
    finally:
        sys.stdin = real_stdin
    assert closed == [True]


def test_cmd_serve_leaves_a_non_storage_object_alone(tmp_path) -> None:
    """Only a real ``Storage`` is closed; anything else is passed through."""
    from continuum.serve import cmd_serve

    class Args:
        # As above: an explicit path keeps the test from opening the default
        # continuum.db in whatever directory pytest runs from.
        db = str(tmp_path / "non_storage.db")
        transport = "stdio"
        port = 8765

    import sys

    real_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        # storage=None is the real shape on a run that did not open a database.
        assert cmd_serve(Args, None, io.StringIO(), io.StringIO()) == 0
    finally:
        sys.stdin = real_stdin


def test_cmd_serve_forwards_the_db_transport_and_port() -> None:
    """The knobs the CLI parses reach ``run_serve`` unchanged."""
    import continuum.serve
    from continuum.serve import cmd_serve

    seen: dict = {}

    def fake_run_serve(
        db=None, transport="stdio", instream=None, outstream=None, port=8765, bind=None
    ):
        seen.update(db=db, transport=transport, port=port, bind=bind)
        return 7

    monkeypatch_target = continuum.serve
    original = monkeypatch_target.run_serve
    monkeypatch_target.run_serve = fake_run_serve
    try:

        class Args:
            db = "runs.db"
            transport = "http"
            port = 9999
            bind = "127.0.0.1"

        class StorageLike:
            pass

        assert cmd_serve(Args, StorageLike(), io.StringIO(), io.StringIO()) == 7
    finally:
        monkeypatch_target.run_serve = original

    assert seen == {"db": "runs.db", "transport": "http", "port": 9999, "bind": "127.0.0.1"}

"""RISK_OBSERVED ingestion and risk-policy mapping (issue #563)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.events import EventType
from continuum.models import Origin, RecoveryMode, Run
from continuum.recovery.risk import (
    RiskPolicy,
    RiskPolicyError,
    evaluate_risk,
    evaluate_risk_action,
    ingest_risk,
    ingest_risk_json_line,
    is_more_conservative,
    load_risk_policy,
)
from continuum.recovery.risk_policy import RISK_POLICY_SCHEMA
from continuum.storage import SQLiteStorage


def test_ingest_risk_records_event(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_ingest.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_ingest"
        store.create_run_started(Run(run_id=run_id, goal="test risk"))
        ok = ingest_risk(store, run_id, {"trigger": "loop", "score": 0.8, "detail": "repetition"})
        assert ok is True
        events = store.read_events(run_id)
        assert events[-1].type == EventType.RISK_OBSERVED
        assert events[-1].source == Origin.EXTERNAL_MONITOR
        assert events[-1].payload["trigger"] == "loop"
        assert events[-1].payload["score"] == 0.8


def test_ingest_risk_fail_open_on_garbage(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_failopen.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_failopen"
        store.create_run_started(Run(run_id=run_id, goal="failopen"))
        # Missing trigger
        assert ingest_risk(store, run_id, {"score": 1}) is False
        # Empty trigger
        assert ingest_risk(store, run_id, {"trigger": ""}) is False
        # Malformed JSON line
        assert ingest_risk_json_line(store, run_id, "not json") is False
        assert ingest_risk_json_line(store, run_id, "") is False
        # No event should have been added beyond RUN_STARTED
        assert len(store.read_events(run_id)) == 1


def test_ingest_risk_keeps_caller_supplied_ts(tmp_path: Path) -> None:
    # Issue #1075: a payload carrying ts but no timestamp took the
    # mint-server-now branch, silently replacing the probe's observation
    # time with CONTINUUM's ingestion time.
    db = str(tmp_path / "risk_ts.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_ts"
        store.create_run_started(Run(run_id=run_id, goal="caller ts"))
        observed = "2020-01-01T00:00:00+00:00"
        assert ingest_risk(store, run_id, {"trigger": "latency_anomaly", "ts": observed}) is True
        assert store.read_events(run_id)[-1].payload["ts"] == observed


def test_ingest_risk_honours_timestamp_spelling(tmp_path: Path) -> None:
    # Issue #1075: a payload carrying timestamp but no ts took neither
    # branch, so the recorded event had no ts at all.
    db = str(tmp_path / "risk_timestamp.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_timestamp"
        store.create_run_started(Run(run_id=run_id, goal="timestamp spelling"))
        observed = "2020-02-02T00:00:00+00:00"
        assert (
            ingest_risk(store, run_id, {"trigger": "token_runaway", "timestamp": observed}) is True
        )
        assert store.read_events(run_id)[-1].payload["ts"] == observed


def test_ingest_risk_mints_ts_only_when_caller_supplies_none(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_mint.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_mint"
        store.create_run_started(Run(run_id=run_id, goal="mint"))
        assert ingest_risk(store, run_id, {"trigger": "loop"}) is True
        assert store.read_events(run_id)[-1].payload.get("ts")


def test_policy_defaults_and_conservative(tmp_path: Path) -> None:
    # Defaults are loaded when file missing
    missing = tmp_path / "no_policy.json"
    policy = load_risk_policy(missing)
    assert policy["loop"] == "replan"
    assert policy["error_cascade"] == "wait"
    assert policy["meltdown"] == "rollback"
    # Conservative check
    assert (
        is_more_conservative("wait", "replan") is True
    )  # WAIT more severe than REPLAN? Check severity ordering
    # Actually SEVERITY: REPLAN=2, WAIT=3, so WAIT > REPLAN, so more conservative
    # Downgrade should be refused
    assert is_more_conservative("replan", "wait") is False
    assert is_more_conservative("annotate", "wait") is False
    assert is_more_conservative("wait", "annotate") is True


def test_policy_file_validation(tmp_path: Path) -> None:
    # Valid file
    p = tmp_path / "risk-policy.json"
    p.write_text(json.dumps({"loop": "replan", "meltdown": "rollback"}), encoding="utf-8")
    policy = load_risk_policy(p)
    assert policy["loop"] == "replan"
    # Invalid mode
    p.write_text(json.dumps({"loop": "invalid_mode"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be one of"):
        load_risk_policy(p)
    # Downgrade attempt: loop defaults to replan, downgrade to annotate should fail
    p.write_text(json.dumps({"loop": "annotate"}), encoding="utf-8")
    with pytest.raises(ValueError, match="downgrades"):
        load_risk_policy(p)
    # Upgrade is allowed: loop replan -> wait is more severe? Check
    # REPLAN=2, WAIT=3, so wait is more severe, should be allowed
    p.write_text(json.dumps({"loop": "wait"}), encoding="utf-8")
    policy2 = load_risk_policy(p)
    assert policy2["loop"] == "wait"


def test_evaluate_risk_mapping(tmp_path: Path) -> None:
    policy = load_risk_policy(tmp_path / "missing.json")
    assert evaluate_risk("loop", policy) == "replan"
    assert evaluate_risk("loop_persisting", policy) == "rollback"
    assert evaluate_risk("error_cascade", policy) == "wait"
    assert evaluate_risk("latency_anomaly", policy) is None
    assert evaluate_risk("meltdown", policy) == "rollback"
    assert evaluate_risk("governance_decay", policy) == "request_human"
    assert evaluate_risk("unknown_trigger", policy) is None
    assert evaluate_risk("", policy) is None


def test_risk_events_are_hash_chained(tmp_path: Path) -> None:
    db = str(tmp_path / "risk_chain.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_chain"
        store.create_run_started(Run(run_id=run_id, goal="chain"))
        ingest_risk(store, run_id, {"trigger": "loop"})
        ingest_risk(store, run_id, {"trigger": "meltdown"})
        report = store.verify_events(run_id)
        assert report.ok is True
        assert report.trusted_through[run_id] == 3


def test_risk_rationale_names_every_trigger_of_the_winning_mode(tmp_path: Path) -> None:
    # Issue #1057: equal-severity triggers each landed in triggering_risks
    # but only the first was named in the rationale that becomes the sealed
    # reason, leaving contributor event ids unexplained.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_multi.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_multi"
        store.create_run_started(Run(run_id=run_id, goal="multi trigger"))
        ingest_risk(store, run_id, {"trigger": "error_cascade", "score": 0.9})
        # Sub-threshold on purpose: at 0.8 token_runaway escalates to abort and
        # stops being a wait-severity contributor to this rationale.
        ingest_risk(store, run_id, {"trigger": "token_runaway", "score": 0.5})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "wait"
    assert len(decision.contract.triggering_risks) == 2
    assert "risk error_cascade, token_runaway triggers wait" in decision.rationale
    assert decision.contract.reason == "risk error_cascade, token_runaway triggers wait"


def test_risk_rationale_names_only_the_winning_mode_contributors(tmp_path: Path) -> None:
    # A trigger proposing a less severe mode is not a contributor: the
    # rollback winner's reason names the rollback trigger alone.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_severe.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_severe"
        store.create_run_started(Run(run_id=run_id, goal="severity ordering"))
        ingest_risk(store, run_id, {"trigger": "error_cascade"})
        ingest_risk(store, run_id, {"trigger": "meltdown"})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "rollback"
    assert decision.contract.reason == "risk meltdown triggers rollback"


def test_risk_rationale_dedupes_a_repeated_trigger(tmp_path: Path) -> None:
    # The same trigger observed twice is one sentence, not two (#1042's
    # failure mode must not be reintroduced by naming every contributor).
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_repeat.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_repeat"
        store.create_run_started(Run(run_id=run_id, goal="repeat trigger"))
        ingest_risk(store, run_id, {"trigger": "error_cascade"})
        ingest_risk(store, run_id, {"trigger": "error_cascade"})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.contract.reason == "risk error_cascade triggers wait"
    assert decision.contract.reason.count("error_cascade") == 1


def test_risk_rationale_locates_repeating_steps(tmp_path: Path) -> None:
    # Issue #1426: a loop verdict that names the trigger but not the steps
    # leaves the replanning agent no way to avoid the repetition it was warned
    # about. The step ids the feed supplied must ride along with the verdict.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_steps.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_steps"
        store.create_run_started(Run(run_id=run_id, goal="located guidance"))
        # Two distinct steps, observed in this order, with a repeat: the
        # guidance must read as one set in first-seen order.
        for step in ("analyze", "report", "analyze"):
            ingest_risk(store, run_id, {"trigger": "loop", "step_id": step})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "replan"
    assert decision.contract.reason == (
        "risk loop triggers replan; repeating steps to avoid: analyze, report"
    )
    assert decision.contract.reason.count("analyze") == 1


def test_risk_rationale_omits_step_guidance_when_feed_gave_none(tmp_path: Path) -> None:
    # A trigger that carries no step id must not gain an empty guidance clause:
    # "repeating steps to avoid:" with nothing after it is not guidance.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_no_steps.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_no_steps"
        store.create_run_started(Run(run_id=run_id, goal="unlocated"))
        ingest_risk(store, run_id, {"trigger": "meltdown", "score": 0.99})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "rollback"
    assert decision.contract.reason == "risk meltdown triggers rollback"
    assert "repeating steps to avoid" not in decision.contract.reason


def test_risk_step_id_from_a_losing_trigger_is_not_guidance(tmp_path: Path) -> None:
    # A less severe trigger loses, so the step it located must not survive into
    # the winner's guidance: guidance describes what the winning verdict covers.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_losing.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_losing"
        store.create_run_started(Run(run_id=run_id, goal="severity wins"))
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "analyze"})
        ingest_risk(store, run_id, {"trigger": "meltdown", "score": 0.99})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "rollback"
    assert decision.contract.reason == "risk meltdown triggers rollback"
    assert "analyze" not in decision.contract.reason


def test_risk_step_id_ignored_when_blank_or_absent(tmp_path: Path) -> None:
    # The feed is fail-open, so a probe that sent a blank or missing step id
    # contributes no location rather than an empty or bogus one. A numeric id
    # is normalized to a string at ingestion, which is a real step reference.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_blank_step.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_blank_step"
        store.create_run_started(Run(run_id=run_id, goal="malformed step id"))
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": "   "})
        ingest_risk(store, run_id, {"trigger": "loop"})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "replan"
    assert "repeating steps to avoid" not in decision.contract.reason

    db = str(tmp_path / "risk_numeric_step.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_numeric_step"
        store.create_run_started(Run(run_id=run_id, goal="numeric step id"))
        ingest_risk(store, run_id, {"trigger": "loop", "step_id": 42})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.contract.reason == ("risk loop triggers replan; repeating steps to avoid: 42")


def test_engine_escalates_token_runaway_at_the_configured_threshold(tmp_path: Path) -> None:
    # The engine used to look up the mapped mode only, so a token_runaway the
    # monitor was 0.95 confident about still proposed wait and an operator's
    # token_runaway_threshold never reached a decision. Assessment now reads
    # the event's score (review of #1422).
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_escalate.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_escalate"
        store.create_run_started(Run(run_id=run_id, goal="escalation"))
        ingest_risk(store, run_id, {"trigger": "token_runaway", "score": 0.95})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "abort"
    assert decision.contract.reason == "risk token_runaway triggers abort"


def test_engine_keeps_a_subthreshold_token_runaway_at_wait(tmp_path: Path) -> None:
    # The escalation is threshold-gated: below 0.8 the mapped mode stands.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_no_escalate.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_no_escalate"
        store.create_run_started(Run(run_id=run_id, goal="no escalation"))
        ingest_risk(store, run_id, {"trigger": "token_runaway", "score": 0.4})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "wait"


def test_engine_falls_back_to_defaults_on_an_invalid_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An invalid policy file used to raise out of the whole risk block, which
    # the broad handler answered by clearing every recorded risk: an unhandled
    # meltdown could then read as safe to resume. Loading now fails over to the
    # conservative defaults instead, so the severe-trigger protections survive a
    # misconfiguration (review of #1422).
    from continuum.observability import RISK_POLICY_LOAD_FALLBACK, get_metrics, reset_metrics
    from continuum.recovery import RecoveryEngine

    # A policy file that the loader must reject: an unknown trigger.
    policy_dir = tmp_path / "policy-cwd"
    policy_dir.mkdir()
    (policy_dir / ".continuum").mkdir()
    (policy_dir / ".continuum" / "risk-policy.json").write_text(
        json.dumps({"meltdown": "rollback", "typo_trigger": "wait"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(policy_dir)
    reset_metrics()

    db = str(tmp_path / "risk_fallback.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_fallback"
        store.create_run_started(Run(run_id=run_id, goal="invalid policy"))
        ingest_risk(store, run_id, {"trigger": "meltdown", "score": 0.9})
        ingest_risk(store, run_id, {"trigger": "side_effect_duplicate", "score": 0.9})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    # The duplicate-side-effect abort survived the rejected policy, and the
    # fallback is visible as a counter rather than silent.
    assert decision.mode.value == "abort"
    assert get_metrics().snapshot()["counters"].get(RISK_POLICY_LOAD_FALLBACK) == 1


# --- Parser fail-closed edge cases (review of #1422) ---


def test_load_risk_policy_fail_closed_on_an_unreadable_file(tmp_path: Path) -> None:
    # A file that cannot even be decoded as text is the same kind of invalid as
    # one whose JSON is broken: the loader rejects it rather than guessing.
    p = tmp_path / "undecodable.json"
    p.write_bytes(b'{"loop": "replan"}\n\xff\xfe not utf-8')
    with pytest.raises(RiskPolicyError, match="invalid risk policy"):
        load_risk_policy(p)


def test_load_risk_policy_rejects_empty_and_blank_keys(tmp_path: Path) -> None:
    # A blank key names no trigger, so it cannot be checked against the
    # baseline; the parser fails closed instead of silently dropping it.
    p = tmp_path / "blank_key.json"
    p.write_text(json.dumps({"": "wait"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="risk policy keys must be non-empty strings"):
        load_risk_policy(p)

    p.write_text(json.dumps({"   ": "wait"}), encoding="utf-8")
    with pytest.raises(RiskPolicyError, match="risk policy keys must be non-empty strings"):
        load_risk_policy(p)


# --- Policy object surface (review of #1422) ---


def test_risk_policy_accepts_the_threshold_as_a_plain_keyword() -> None:
    # The keyword and the **kwargs spellings must land in the same slot: an
    # operator passing the threshold through a mapping built at runtime gets
    # the same validation and the same value.
    policy = RiskPolicy(**{"token_runaway_threshold": 0.5})  # type: ignore[arg-type]
    assert policy.token_runaway_threshold == 0.5
    assert policy["token_runaway"] == "wait"
    assert policy["meltdown"] == "rollback"


def test_risk_policy_json_schema_matches_the_generated_one() -> None:
    # The classmethod is the public way to fetch the schema a validator
    # downloads, so it must return exactly the generated model schema.
    assert RiskPolicy.json_schema() == RISK_POLICY_SCHEMA


def test_risk_policy_to_dict_exposes_the_mapping_and_threshold() -> None:
    # Serialising must carry the threshold alongside the trigger mapping,
    # because the threshold is part of the policy a consumer reloads.
    policy = RiskPolicy({"meltdown": "abort"})
    as_dict = policy.to_dict()
    assert isinstance(as_dict, dict)
    assert as_dict["meltdown"] == "abort"
    assert as_dict["loop"] == "replan"
    assert as_dict["token_runaway_threshold"] == 0.8


def test_is_more_conservative_falls_back_for_unknown_modes() -> None:
    # Both orderings of a mode the severity table does not know end up False:
    # an unknown mode is never quietly accepted as more conservative, and the
    # engine-SEVERITY fallback keeps failures rather than guessing.
    assert is_more_conservative("rollback", "not_a_mode") is False
    assert is_more_conservative("not_a_mode", "wait") is False


# --- Score-aware evaluator edge cases (review of #1422) ---


def test_evaluate_risk_action_ignores_a_non_string_trigger() -> None:
    # The feed is fail-open, so a trigger that is not text proposes nothing
    # rather than being looked up as a mode.
    assert evaluate_risk_action(None) is None  # type: ignore[arg-type]
    assert evaluate_risk_action(42) is None  # type: ignore[arg-type]


def test_evaluate_risk_action_reads_the_threshold_from_a_plain_dict() -> None:
    # A policy passed as a plain mapping carries its threshold as a key the
    # evaluator has to read, not only as a typed RiskPolicy attribute.
    policy = {"token_runaway": "wait", "token_runaway_threshold": 0.5}
    assert evaluate_risk_action("token_runaway", 0.49, policy) is RecoveryMode.WAIT
    assert evaluate_risk_action("token_runaway", 0.50, policy) is RecoveryMode.ABORT


def test_evaluate_risk_action_falls_back_to_the_default_threshold_when_unparseable() -> None:
    # A threshold the evaluator cannot parse is not a free pass to escalate on
    # every score: evaluation falls back to the 0.8 baseline, so 0.79 stays
    # WAIT and 0.9 still escalates.
    policy = {"token_runaway": "wait", "token_runaway_threshold": "not-a-number"}
    assert evaluate_risk_action("token_runaway", 0.79, policy) is RecoveryMode.WAIT
    assert evaluate_risk_action("token_runaway", 0.90, policy) is RecoveryMode.ABORT


def test_evaluate_risk_action_returns_none_for_an_unmapped_mode() -> None:
    # A plain mapping can hold a mode string the RecoveryMode enum rejects;
    # the evaluator then proposes nothing instead of raising or coercing.
    assert evaluate_risk_action("loop", 0.5, {"loop": "replanish"}) is None


# --- Engine risk evaluation edge cases (review of #1422) ---


def test_a_trigger_proposing_a_weaker_mode_after_the_winner_is_ignored(tmp_path: Path) -> None:
    # Severity decides, not arrival order: a signal that arrives after a more
    # severe winner and maps below it contributes nothing, keeping the winning
    # verdict's event ids and reason unpolluted.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_weaker.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_weaker"
        store.create_run_started(Run(run_id=run_id, goal="weaker late signal"))
        ingest_risk(store, run_id, {"trigger": "meltdown", "score": 0.9})
        ingest_risk(store, run_id, {"trigger": "error_cascade", "score": 0.9})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "rollback"
    assert decision.contract.reason == "risk meltdown triggers rollback"
    assert len(decision.contract.triggering_risks) == 1


def test_a_risk_event_with_a_non_string_trigger_contributes_nothing(tmp_path: Path) -> None:
    # ingest_risk normalises the trigger through the payload schema, but an
    # event written by another path can still carry a non-text trigger; the
    # evaluation must skip it rather than raise or mistake it for a mode.
    from continuum.recovery import RecoveryEngine

    db = str(tmp_path / "risk_raw.db")
    with SQLiteStorage(db) as store:
        run_id = "run_risk_raw"
        store.create_run_started(Run(run_id=run_id, goal="raw event"))
        store.append_event(
            run_id,
            EventType.RISK_OBSERVED,
            {"trigger": 42, "score": 0.9},
            source=Origin.EXTERNAL_MONITOR,
        )
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.contract.triggering_risks == []
    # The malformed event drove no risk verdict: no trigger-shaped rationale
    # sentence was produced for it (the unrelated projection prose may still
    # mention the word "risk" for other reasons).
    assert "triggers" not in (decision.contract.reason or "")


def test_engine_survives_when_the_fallback_counter_cannot_be_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fallback counter is observability, not control flow: even when the
    # metrics call itself fails, the rejected policy still fails over to the
    # conservative defaults and the severe-trigger protections survive.
    import continuum.observability as observability
    from continuum.recovery import RecoveryEngine

    attempts: list[str] = []

    def broken_get_metrics() -> None:
        attempts.append("called")
        raise RuntimeError("metrics unavailable")

    policy_dir = tmp_path / "policy-cwd"
    (policy_dir / ".continuum").mkdir(parents=True)
    (policy_dir / ".continuum" / "risk-policy.json").write_text(
        json.dumps({"typo_trigger": "wait"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(policy_dir)
    monkeypatch.setattr(observability, "get_metrics", broken_get_metrics)

    db = str(tmp_path / "metrics_broken.db")
    with SQLiteStorage(db) as store:
        run_id = "run_metrics_broken"
        store.create_run_started(Run(run_id=run_id, goal="metrics broken"))
        ingest_risk(store, run_id, {"trigger": "side_effect_duplicate", "score": 0.9})
        decision = RecoveryEngine(store).assess(run_id, replay=False)

    assert decision.mode.value == "abort"
    # The increment was attempted and every attempt raised, yet the severe
    # trigger's protection survived: the counter is observability only.
    assert attempts

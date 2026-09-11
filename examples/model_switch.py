"""Model switch: Model A disappears, Model B resumes.

    python examples/model_switch.py

An agent starts work using Model A (gpt-4-turbo). CONTINUUM checkpoints its
state, including a model-specific assumption. Model A then becomes
unavailable. Model B (gpt-4o) attempts to resume.

CONTINUUM does NOT assume the switch is safe. The model-specific state
recorded under Model A is flagged, and the recovery decision reflects that
Model B must revalidate that assumption before treating it as trustworthy.
"""

from __future__ import annotations

import sys
from pathlib import Path

BANNER = "=" * 68

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from continuum import (  # noqa:  E402
    CheckpointManager,
    RecoveryEngine,
    SemanticPolicy,
    SemanticState,
    SQLiteStorage,
    StateValidator,
    build_recovery_context,
    capture_environment,
)
from continuum.checkpoint.context import estimate_tokens  # noqa:  E402
from continuum.environment import StaticProvider  # noqa:  E402
from continuum.models import (  # noqa:  E402
    Decision,
    Evidence,
    ExternalDependency,
    Finding,
    Goal,
    ModelSpecificState,
    ModelState,
    PendingWork,
    Progress,
)


def say(text: str = "") -> None:
    print(text, flush=True)


def heading(text: str) -> None:
    say()
    say(BANNER)
    say(text)
    say(BANNER)


def build_agent_state(run_id: str, model_name: str) -> SemanticState:
    """Build state as it would exist after partial work under a given model."""
    goal = Goal(
        description="Evaluate 500 patient records for treatment efficacy",
        version=2,
        constraints=["HIPAA-compliant handling", "peer-reviewed criteria only"],
    )

    progress = Progress(total=500, completed=234, pending=263, failed=3)

    decisions = [
        Decision(
            decision_id="dec_001",
            decision="Use double-blind assessment protocol",
            reason="Reduces observer bias",
            evidence=["record_042"],
        ),
        Decision(
            decision_id="dec_002",
            decision="Exclude patients with comorbidities from primary analysis",
            reason="Confounds efficacy measurement",
            evidence=["finding_001"],
        ),
    ]

    evidence = [
        Evidence(
            evidence_id="record_042",
            summary="Patient 042: positive response to treatment A (p<0.01)",
            source="ehr_system",
            checksum="sha256:r042",
        ),
        Evidence(
            evidence_id="record_128",
            summary="Patient 128: no significant response, dropped from study",
            source="ehr_system",
            checksum="sha256:r128",
        ),
    ]

    findings = [
        Finding(
            finding_id="finding_001",
            claim="Treatment A shows 67% efficacy in non-comorbid patients",
            evidence=["record_042"],
            confidence=0.82,
        ),
        Finding(
            finding_id="finding_002",
            claim="Comorbidity is the strongest negative predictor",
            evidence=["record_128"],
            confidence=0.71,
        ),
    ]

    pending_work = [
        PendingWork(task_id="task_001", description="Complete assessment of records 235–350"),
        PendingWork(task_id="task_002", description="Run statistical significance tests"),
        PendingWork(task_id="task_003", description="Draft efficacy report"),
    ]

    external_dependencies = [
        ExternalDependency(
            resource="ehr_system",
            kind="api",
            version="v3.2",
            checksum="sha256:ehr_v3.2",
        ),
        ExternalDependency(
            resource="criteria_db",
            kind="resource",
            version="2024.1",
        ),
    ]

    model = ModelState(
        model=model_name,
        provider="openai",
        fingerprint="fp_" + model_name.replace("-", "_"),
        model_specific_state=[
            ModelSpecificState(
                item_id="ms_cot",
                description=(
                    f"{model_name} was instructed to produce structured "
                    "chain-of-thought reasoning with confidence scores"
                ),
                required_validation=(
                    "Must revalidate: new model may not produce CoT in the "
                    "same format or with the same calibration"
                ),
            ),
            ModelSpecificState(
                item_id="ms_calibration",
                description=(
                    f"Confidence scores were calibrated against {model_name}'s "
                    "known output distribution"
                ),
                required_validation=(
                    "Must revalidate: confidence scores from a different model "
                    "are not directly comparable"
                ),
            ),
        ],
    )

    return SemanticState(
        run_id=run_id,
        goal=goal,
        progress=progress,
        decisions=decisions,
        evidence=evidence,
        findings=findings,
        pending_work=pending_work,
        external_dependencies=external_dependencies,
        model=model,
        source_sequence=87,
    )


def main() -> int:
    db_path = Path(__file__).resolve().parents[1] / "demo-run" / "model_switch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    storage = SQLiteStorage(str(db_path))
    manager = CheckpointManager(storage, policy=SemanticPolicy())
    run_id = "run_model_switch_001"

    MODEL_A = "gpt-4-turbo"
    MODEL_B = "gpt-4o"

    from continuum.models import Run, RunStatus

    heading("1. Model A executes part of the task")
    state_a = build_agent_state(run_id, MODEL_A)

    storage.create_run(Run(run_id=run_id, goal=state_a.goal.description, status=RunStatus.STARTED))

    env = capture_environment(run_id, StaticProvider(ehr_system="v3.2", criteria_db="2024.1"))
    checkpoint = manager.checkpoint(
        run_id, state=state_a, environment=env, reason="model A checkpoint"
    )

    say(f"    Model: {MODEL_A}")
    say(f"    Goal: {state_a.goal.description}")
    say(f"    Progress: {state_a.progress.completed}/{state_a.progress.total} records assessed")
    say(f"    Decisions: {len(state_a.decisions)}")
    say(f"    Findings: {len(state_a.findings)}")
    say(f"    Pending work: {len(state_a.pending_work)} items")
    say(f"    Model-specific assumptions: {len(state_a.model.model_specific_state)}")
    for mss in state_a.model.model_specific_state:
        say(f"      - {mss.description}")
    say(f"    Checkpoint: v{checkpoint.version}")
    say()

    heading("2. Model A becomes unavailable")
    say(f"    [{MODEL_A} API returns 503: capacity exhausted]")
    say("    [The agent must switch models to continue]")
    say()

    heading("3. Attempting recovery with Model B (no model check)")
    say(f"    Validating state against current environment, still claiming {MODEL_A}...")
    validator_same = StateValidator()
    outcome_same = validator_same.validate(
        state_a,
        current_environment=env,
        checkpoint_environment=env,
        checkpoint_version=checkpoint.version,
        expected_model=MODEL_A,
    )
    say(f"    Safe to resume (same model): {'yes' if outcome_same.safe else 'no'}")
    say(f"    Reason: {outcome_same.report.reason}")
    say()

    heading("4. Recovery with Model B: CONTINUUM detects the switch")
    say(f"    Now validating with expected_model={MODEL_B}...")
    engine = RecoveryEngine(storage)
    decision = engine.assess(
        run_id,
        current_environment=env,
        expected_model=MODEL_B,
    )

    say(f"    Recovery decision: {decision.mode.value.upper()}")
    say(f"    Safe to resume: {'yes' if decision.safe else 'no'}")
    for reason in decision.rationale:
        say(f"      because {reason}")
    say()

    heading("5. What Model B must revalidate before trusting")
    say("    The following model-specific state was recorded under Model A")
    say("    and is now flagged:")

    for entry in decision.validation.report.statuses:
        if entry.component.value == "model":
            say(f"      [{entry.status}] {entry.detail}")
    say()

    for mss in decision.state.model.model_specific_state:
        say(f"      Model-specific assumption: {mss.description}")
        say(f"        Required validation: {mss.required_validation}")
    say()

    heading("6. Recovery context for Model B")
    recovery_context = build_recovery_context(
        decision.state,
        next_action=decision.next_allowed_action,
    )
    say(recovery_context.render())
    say()
    say(f"    Recovery context size: {estimate_tokens(recovery_context.render())} tokens (est.)")
    say()

    heading("7. What CONTINUUM does NOT assume")
    say("    CONTINUUM does NOT assume that switching models is automatically safe.")
    say("    Specifically:")
    say("      - Model-specific state is flagged, not silently carried forward")
    say("      - The recovery decision reflects the model change")
    say("      - Model B is told what it must revalidate, not what to trust")
    say("      - The agent cannot resume without acknowledging the switch")
    say()

    if not decision.safe:
        say("    RESULT: Recovery correctly BLOCKED until Model B revalidates")
        say("    the model-specific assumptions recorded under Model A.")
    else:
        say("    RESULT: Recovery permitted, but model-specific state is marked")
        say("    for review, not silently trusted.")

    say()
    say(f"    Database: {db_path}")
    say("    Cleanup:   rm -rf demo-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Context compaction: the transcript is lost, the work is not.

    python examples/context_compaction.py

An agent processes a long sequence of items over many turns. Its context
window fills up, the platform compacts it, and the full conversation
history is gone. What survives is CONTINUUM's semantic checkpoint, and
from that alone it reconstructs a bounded recovery context sufficient to
continue the task.

This measures the actual compression: full transcript size (characters,
labelled as the heuristic estimate the codebase uses) versus the recovery
context size, and reports whether the task can correctly resume.
"""

from __future__ import annotations

import sys
from pathlib import Path

BANNER = "=" * 68

# Make the import work whether run from the repo root or directly.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from continuum import (  # noqa:  E402
    CheckpointManager,
    RecoveryEngine,
    SemanticPolicy,
    SemanticState,
    SQLiteStorage,
    build_recovery_context,
    capture_environment,
)
from continuum.checkpoint.context import estimate_tokens  # noqa:  E402
from continuum.environment import StaticProvider  # noqa:  E402
from continuum.models import (  # noqa:  E402
    Approval,
    ApprovalStatus,
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


def build_rich_state(run_id: str) -> SemanticState:
    """Build a realistic agent state with many components."""
    goal = Goal(
        description="Analyze 2,000 research papers for evidence supporting or refuting hypothesis X",
        version=3,
        constraints=["peer-reviewed only", "2019-2022 publication window"],
    )

    progress = Progress(total=2_000, completed=1_247, pending=741, failed=12)

    decisions = [
        Decision(
            decision_id="dec_001",
            decision="Exclude non-English papers",
            reason="Cannot verify translations",
            evidence=["paper_042"],
        ),
        Decision(
            decision_id="dec_002",
            decision="Weight double-blind studies 2x",
            reason="Higher evidence quality",
            evidence=["finding_003"],
        ),
        Decision(
            decision_id="dec_003",
            decision="Flag contradictory evidence for manual review",
            reason="Inconsistent results across cohorts",
            evidence=["paper_128", "paper_256"],
        ),
    ]

    evidence = [
        Evidence(
            evidence_id="paper_042",
            summary="Smith et al. 2020: X holds in 73% of cases (n=4,200)",
            source="dataset_v2",
            checksum="sha256:a1b2c3",
        ),
        Evidence(
            evidence_id="paper_128",
            summary="Jones et al. 2021: X does not hold in replication (n=800)",
            source="dataset_v2",
            checksum="sha256:d4e5f6",
        ),
        Evidence(
            evidence_id="paper_256",
            summary="Lee et al. 2022: X partially holds, moderated by factor Y",
            source="dataset_v2",
            checksum="sha256:g7h8i9",
        ),
    ]

    findings = [
        Finding(
            finding_id="finding_001",
            claim="Hypothesis X is broadly supported but effect size varies",
            evidence=["paper_042", "paper_256"],
            confidence=0.78,
        ),
        Finding(
            finding_id="finding_002",
            claim="Replication crisis affects 30% of X-related studies",
            evidence=["paper_128"],
            confidence=0.65,
        ),
        Finding(
            finding_id="finding_003",
            claim="Double-blind methodology reduces false positives by 40%",
            evidence=["paper_042"],
            confidence=0.91,
        ),
    ]

    pending_work = [
        PendingWork(
            task_id="task_001", description="Resolve contradictory evidence from Jones et al."
        ),
        PendingWork(task_id="task_002", description="Complete analysis of papers 1,248–1,500"),
        PendingWork(task_id="task_003", description="Finalize confidence weighting methodology"),
    ]

    approvals = [
        Approval(
            approval_id="approval_001",
            subject="Exclude non-English papers",
            status=ApprovalStatus.GRANTED,
            granted_by="user@example.com",
        ),
    ]

    external_dependencies = [
        ExternalDependency(
            resource="dataset",
            kind="resource",
            version="v2",
            checksum="sha256:dataset_v2_full",
            last_verified_at=None,
        ),
        ExternalDependency(
            resource="openai_api",
            kind="api",
            version="2024-01",
        ),
    ]

    model = ModelState(
        model="gpt-4-turbo",
        provider="openai",
        fingerprint="abc123",
        model_specific_state=[
            ModelSpecificState(
                item_id="ms_001",
                description="Model was instructed to use chain-of-thought reasoning",
                required_validation="Must revalidate if model does not support CoT",
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
        approvals=approvals,
        external_dependencies=external_dependencies,
        model=model,
        source_sequence=153,
    )


def simulate_transcript(state: SemanticState) -> str:
    """Build a plausible full transcript from the state (what compaction loses)."""
    lines = [
        "System: You are a research analysis agent.",
        f"User: {state.goal.description}",
        "Assistant: I'll analyze the papers systematically. Let me start.",
    ]

    for i in range(state.progress.completed):
        lines.append(f"User: Process paper {i}")
        lines.append(
            f"Assistant: Paper {i} processed. "
            f"Key finding: {'supports X' if i % 3 != 0 else 'mixed evidence'}. "
            f"Confidence: {0.5 + (i % 5) * 0.1:.2f}. "
            f"Methodology notes: double-blind={i % 2 == 0}, "
            f"sample_size={100 + i * 3}, replication={'yes' if i % 4 == 0 else 'no'}."
        )
        if i % 50 == 0 and i > 0:
            lines.append(
                f"Assistant: Progress update: {i} papers done. "
                f"Current trend: hypothesis X broadly supported but "
                f"effect size varies. {state.progress.total - i} remaining."
            )

    lines.append(f"User: {state.goal.description} (reiterated after compaction)")
    lines.append("Assistant: Let me continue from where I left off.")
    return "\n".join(lines)


def main() -> int:
    db_path = Path(__file__).resolve().parents[1] / "demo-run" / "compaction.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    storage = SQLiteStorage(str(db_path))
    manager = CheckpointManager(storage, policy=SemanticPolicy())
    run_id = "run_compaction_001"

    heading("1. Agent works and builds rich semantic state")
    state = build_rich_state(run_id)

    from continuum.models import Run, RunStatus

    storage.create_run(Run(run_id=run_id, goal=state.goal.description, status=RunStatus.STARTED))

    env = capture_environment(run_id, StaticProvider(dataset="v2", openai_api="2024-01"))
    checkpoint = manager.checkpoint(
        run_id, state=state, environment=env, reason="semantic checkpoint"
    )

    say(f"    Goal: {state.goal.description}")
    say(f"    Progress: {state.progress.completed}/{state.progress.total} papers analyzed")
    say(f"    Decisions: {len(state.decisions)}")
    say(f"    Findings: {len(state.findings)}")
    say(f"    Evidence items: {len(state.evidence)}")
    say(f"    Pending work items: {len(state.pending_work)}")
    say(f"    Checkpoint version: v{checkpoint.version}")
    say()

    heading("2. The context window fills up; platform compacts the transcript")
    transcript = simulate_transcript(state)
    transcript_chars = len(transcript)
    transcript_tokens = estimate_tokens(transcript)

    say("    Original transcript size:")
    say(f"      characters:         {transcript_chars:,}")
    say(f"      est. tokens (heuristic, chars/4): {transcript_tokens:,}")
    say()
    say("    [compaction occurs; full conversation history is discarded]")
    say("    [the LLM can no longer see any previous turn, tool call, or reasoning]")
    say()

    heading("3. What survives: the semantic checkpoint")
    say("    The checkpoint was written BEFORE compaction. It contains:")
    say(f"      - Goal (v{state.goal.version}): {state.goal.description}")
    say(f"      - Progress: {state.progress.completed} completed, {state.progress.pending} pending")
    say(f"      - {len(state.decisions)} decisions with evidence trails")
    say(f"      - {len(state.findings)} findings ranked by confidence")
    say(f"      - {len(state.pending_work)} pending work items")
    say(f"      - {len(state.external_dependencies)} external dependencies")
    say(f"      - Model: {state.model.model if state.model else 'none'}")
    say(
        f"      - {len(state.model.model_specific_state) if state.model else 0} model-specific assumption(s)"
    )
    say()

    heading("4. Recovery context reconstructed from checkpoint alone")
    manager.restore(run_id)
    engine = RecoveryEngine(storage)
    decision = engine.assess(run_id, current_environment=env)

    recovery_context = build_recovery_context(
        decision.state,
        next_action=decision.next_allowed_action,
    )

    recovery_text = recovery_context.render()
    recovery_chars = len(recovery_text)
    recovery_tokens = estimate_tokens(recovery_text)

    say(recovery_text)
    say()

    heading("5. Measured compression")
    ratio = transcript_chars / recovery_chars if recovery_chars > 0 else float("inf")
    say(
        f"    Original transcript:  {transcript_chars:>8,} chars (~{transcript_tokens:>6,} tokens est.)"
    )
    say(
        f"    Recovery context:     {recovery_chars:>8,} chars (~{recovery_tokens:>6,} tokens est.)"
    )
    say(f"    Compression ratio:    {ratio:>8.1f}x  (characters)")
    say()

    heading("6. Can the task correctly continue?")
    say("    From the recovery context alone, a resuming agent knows:")
    say(f"      1. What it was doing: {decision.state.goal.description}")
    say(
        f"      2. How far it got:    {decision.state.progress.completed}/{decision.state.progress.total}"
    )
    say(f"      3. What's pending:    {len(decision.state.open_work())} items")
    say(
        f"      4. What to distrust:  {len([d for d in decision.state.decisions if d.status.value != 'valid'])} stale decisions"
    )
    say(
        f"      5. What needs review: {len([f for f in decision.state.findings if f.status.value == 'requires_review'])} findings"
    )
    say(
        f"      6. Safe to resume:    {'yes' if decision.safe else 'no - ' + '; '.join(decision.rationale)}"
    )
    say()

    if decision.safe and decision.state.progress.completed == state.progress.completed:
        say("    RESULT: Task can continue correctly. No work needs to be repeated.")
        say("    The semantic checkpoint carried everything that matters;")
        say("    the transcript carried everything that doesn't need to survive.")
    else:
        say("    RESULT: Recovery blocked; see rationale above.")

    say()
    say(f"    Database: {db_path}")
    say("    Cleanup:   rm -rf demo-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

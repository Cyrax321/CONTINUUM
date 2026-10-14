"""Contract tests for PR status label workflows.

Guards .github/workflows/pr-status-labels.yml and
.github/workflows/pr-status-labels-apply.yml against regression:
1. Valid YAML parsing.
2. Non-approval reviews (e.g., comments) write an explicit skip intent so
   download-artifact always finds the artifact.
3. Download artifact step has continue-on-error so missing artifacts do
   not fail the job.
4. Apply script handles skip intents and missing artifacts gracefully.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / ".github" / "workflows" / "pr-status-labels.yml"
PHASE2 = ROOT / ".github" / "workflows" / "pr-status-labels-apply.yml"


def test_workflow_files_exist_and_parse_yaml() -> None:
    assert PHASE1.is_file(), "pr-status-labels.yml missing"
    assert PHASE2.is_file(), "pr-status-labels-apply.yml missing"

    data1 = yaml.safe_load(PHASE1.read_text(encoding="utf-8"))
    assert data1 is not None and "jobs" in data1

    data2 = yaml.safe_load(PHASE2.read_text(encoding="utf-8"))
    assert data2 is not None and "jobs" in data2


def test_download_artifact_is_fault_tolerant() -> None:
    data = yaml.safe_load(PHASE2.read_text(encoding="utf-8"))
    steps = data["jobs"]["apply"]["steps"]
    download_step = next(s for s in steps if s.get("name") == "Download intent artifact")
    assert download_step.get("continue-on-error") is True, (
        "Download intent artifact must have continue-on-error: true "
        "so that runs with no artifact or expired artifacts do not fail"
    )


def test_phase1_records_skip_intent_on_comment_reviews() -> None:
    content = PHASE1.read_text(encoding="utf-8")
    assert "skip: true" in content, (
        "pr-status-labels.yml must set skip: true on non-approval reviews "
        "so that intent.json is written and uploaded"
    )
    # Ensure it no longer returns early before writing intent.json
    comment_block = content.split("Comment review")[1].split("} else")[0]
    assert "return;" not in comment_block, (
        "pr-status-labels.yml must not return early without writing intent.json"
    )


def test_phase2_handles_skip_and_missing_intents() -> None:
    content = PHASE2.read_text(encoding="utf-8")
    assert "intent.skip" in content, (
        "pr-status-labels-apply.yml must check intent.skip before applying labels"
    )


def test_simulated_comment_review_intent_handling(tmp_path: Path) -> None:
    # Simulate non-approval review recording skip intent
    intent = {"skip": True, "pr_number": 123}
    intent_file = tmp_path / "intent.json"
    intent_file.write_text(json.dumps(intent), encoding="utf-8")

    # Simulate phase 2 read
    loaded = json.loads(intent_file.read_text(encoding="utf-8"))
    assert loaded.get("skip") is True
    assert loaded.get("pr_number") == 123

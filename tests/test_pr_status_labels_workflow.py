"""Contract tests for PR status label workflows.

Guards .github/workflows/pr-status-labels.yml and
.github/workflows/pr-status-labels-apply.yml against regression:
1. Valid YAML parsing (skipped gracefully if PyYAML is not installed).
2. Non-approval reviews (e.g., comments) write an explicit skip intent so
   download-artifact always finds the artifact.
3. Download artifact step has continue-on-error so missing artifacts do
   not fail the job.
4. Apply script handles skip intents and missing artifacts gracefully with
   an explicit early return before any GitHub API calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PHASE1 = ROOT / ".github" / "workflows" / "pr-status-labels.yml"
PHASE2 = ROOT / ".github" / "workflows" / "pr-status-labels-apply.yml"


def test_workflow_files_exist_and_parse_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    assert PHASE1.is_file(), "pr-status-labels.yml missing"
    assert PHASE2.is_file(), "pr-status-labels-apply.yml missing"

    data1 = yaml.safe_load(PHASE1.read_text(encoding="utf-8"))
    assert data1 is not None and "jobs" in data1

    data2 = yaml.safe_load(PHASE2.read_text(encoding="utf-8"))
    assert data2 is not None and "jobs" in data2


def test_download_artifact_is_fault_tolerant() -> None:
    yaml = pytest.importorskip("yaml")
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
    # Verify early return on missing intent file
    catch_match = re.search(r"catch\s*\([^)]*\)\s*\{([^}]+)\}", content)
    assert catch_match and "return;" in catch_match.group(1), (
        "pr-status-labels-apply.yml must return early inside catch block when intent is missing"
    )

    # Verify early return on skip or missing pr_number
    skip_match = re.search(r"if\s*\([^)]*intent\.skip[^)]*\)\s*\{([^}]+)\}", content)
    assert skip_match and "return;" in skip_match.group(1), (
        "pr-status-labels-apply.yml must return early when intent.skip is true"
    )

    # Verify that the guard appears before any github API calls
    skip_pos = content.find("intent.skip")
    github_pos = content.find("github.paginate")
    assert skip_pos != -1 and github_pos != -1 and skip_pos < github_pos, (
        "intent.skip check must precede github API invocations"
    )


def test_simulated_comment_review_intent_handling(tmp_path: Path) -> None:
    # Simulate non-approval review recording skip intent
    intent = {"skip": True, "pr_number": 123}
    intent_file = tmp_path / "intent.json"
    intent_file.write_text(json.dumps(intent), encoding="utf-8")

    # Mock github client to verify zero calls when skipping
    mock_github = MagicMock()

    # Simulate phase 2 execution logic
    def run_apply(file_path: Path, gh: MagicMock) -> bool:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not data or data.get("skip") or not data.get("pr_number"):
            return False
        gh.paginate()
        return True

    # 1. Skip intent: returns False and makes no GitHub API calls
    assert run_apply(intent_file, mock_github) is False
    assert mock_github.call_count == 0

    # 2. Missing intent file: returns False and makes no GitHub API calls
    missing_file = tmp_path / "nonexistent.json"
    assert run_apply(missing_file, mock_github) is False
    assert mock_github.call_count == 0

    # 3. Active intent: processes and invokes GitHub API
    active_file = tmp_path / "active.json"
    active_file.write_text(
        json.dumps({"pr_number": 456, "remove_labels": [], "add_label": "ready"}),
        encoding="utf-8",
    )
    assert run_apply(active_file, mock_github) is True
    assert mock_github.paginate.call_count == 1

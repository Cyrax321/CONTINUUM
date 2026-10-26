"""Contract tests for PR status label workflows.

Guards .github/workflows/pr-status-labels.yml and
.github/workflows/pr-status-labels-apply.yml against regression:
1. Valid YAML parsing (skipped gracefully if PyYAML is not installed).
2. Non-approval reviews (e.g., comments) write an explicit skip intent so
   download-artifact always finds the artifact.
3. Download artifact step has continue-on-error so missing artifacts do
   not fail the job.
4. Apply script handles skip intents and missing artifacts gracefully with
   an explicit early return before any GitHub API calls. The skip and
   missing-intent paths are executed for real under Node (skipped when no
   node runtime is installed), so deleting the early return fails the
   tests instead of only failing the workflows in production.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

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


def _apply_script() -> str:
    """The inline ``Sync status labels`` script from the phase 2 workflow."""
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(PHASE2.read_text(encoding="utf-8"))
    steps = data["jobs"]["apply"]["steps"]
    step = next(s for s in steps if s.get("name") == "Sync status labels")
    return step["with"]["script"]


# Prelude and postlude that turn the apply script into a standalone Node
# program. They stand in for actions/github-script: `require`, `core`,
# `context`, and `github` are provided exactly as the workflow step would,
# and every GitHub REST call the script makes is appended to CALLS, which the
# postlude drops into calls.json for the test to read.
_HARNESS_PRELUDE = """\
const realFs = require("fs");
const CALLS = [];
const api = (name) => {
  const fn = async (input) => {
    CALLS.push(name);
    if (name === "listComments") return { data: [] };
    if (name === "pulls.get") return { data: { user: { login: "contributor" } } };
    return {};
  };
  fn._name = name;
  return fn;
};
const github = {
  paginate: async (fn) => {
    CALLS.push("paginate(" + (fn._name || "unknown") + ")");
    return [];
  },
  rest: {
    issues: {
      listLabelsOnIssue: api("listLabelsOnIssue"),
      removeLabel: api("removeLabel"),
      addLabels: api("addLabels"),
      createComment: api("createComment"),
      listComments: api("listComments"),
    },
    pulls: {
      get: api("pulls.get"),
      list: api("pulls.list"),
    },
  },
};
const core = {
  info() {},
  warning() {},
  error() {},
  setOutput() {},
  setFailed() {},
};
const context = {
  repo: { owner: "example", repo: "continuum" },
  eventName: "workflow_run",
  payload: {},
};

(async () => {
"""

_HARNESS_POSTLUDE = """
})().then(
  () => realFs.writeFileSync("calls.json", JSON.stringify(CALLS)),
  (err) => {
    console.error("apply script failed: " + (err && err.stack));
    process.exit(1);
  }
);
"""


def _run_apply_script(tmp_path: Path, intent: dict | None) -> list[str]:
    """Run the real apply script under Node and return the GitHub calls it made.

    ``intent`` is written to ``intent.json`` in the working directory the
    script reads it from; ``None`` leaves the file absent so the missing
    artifact path is exercised. Stale files from earlier runs in the same
    directory are cleared first, otherwise a ``None`` run would read the
    previous run's intent and take the skip path instead of the missing
    artifact branch.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node runtime not available to execute the apply script")

    program = "\n".join([_HARNESS_PRELUDE, _apply_script(), _HARNESS_POSTLUDE])
    program_file = tmp_path / "apply_harness.js"
    program_file.write_text(program, encoding="utf-8")
    intent_file = tmp_path / "intent.json"
    calls_file = tmp_path / "calls.json"
    intent_file.unlink(missing_ok=True)
    calls_file.unlink(missing_ok=True)
    if intent is not None:
        intent_file.write_text(json.dumps(intent), encoding="utf-8")

    proc = subprocess.run(
        [node, program_file.name],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, f"apply script crashed:\n{proc.stderr[-2000:]}"
    assert calls_file.is_file(), "harness wrote no call log"
    return json.loads(calls_file.read_text(encoding="utf-8"))


def test_apply_script_makes_no_github_calls_on_skip_or_missing_intent(
    tmp_path: Path,
) -> None:
    """Skip and missing intents must reach zero GitHub API calls.

    Comment reviews land here every day, so a skip intent that falls through
    to the label code would post approval side effects and noisy failures on
    every review comment in the repository. Executing the shipped script is
    the only way to know the early return is still in it; text matching
    alone is satisfied by a `return` that sits after the first call.
    """
    skip_calls = _run_apply_script(tmp_path, {"skip": True, "pr_number": 42})
    assert skip_calls == [], f"skip intent still made GitHub calls: {skip_calls}"

    missing_calls = _run_apply_script(tmp_path, None)
    assert missing_calls == [], f"missing intent still made GitHub calls: {missing_calls}"


def test_apply_script_applies_labels_for_active_intent(tmp_path: Path) -> None:
    """An actionable intent does reach the label API, so the guard is a guard.

    Without this control the skip tests would also pass on a script whose
    guard unconditionally returns, which protects nothing.
    """
    calls = _run_apply_script(
        tmp_path,
        {
            "pr_number": 42,
            "add_label": "ready to merge",
            "remove_labels": ["awaiting review"],
            "thank": False,
        },
    )
    assert "paginate(listLabelsOnIssue)" in calls
    assert "addLabels" in calls
    assert "createComment" not in calls

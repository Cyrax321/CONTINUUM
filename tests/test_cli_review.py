"""Tests for the `continuum review` CLI command (issue #1410).

Covers listing ranked pending reviews, bulk-approving low-risk items,
approving individual items, machine-readable --json output, and error conditions.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.cli.exitcodes import ExitCode
from continuum.cli.main import build_parser
from continuum.models import Run
from continuum.recovery.review_queue import ReviewQueue
from continuum.storage.sqlite import SQLiteStorage


@pytest.fixture
def storage(tmp_path: Path) -> SQLiteStorage:
    db = tmp_path / "test_cli.db"
    store = SQLiteStorage(f"sqlite:///{db}")
    store.create_run(Run(run_id="run_1", goal="cli review test"))
    return store


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    p = tmp_path / "escalation.json"
    p.write_text(
        json.dumps(
            {
                "hourly_prompt_cap": 10,
                "batch_window_seconds": 3600,
                "blast_radius_threshold": 0.8,
                "risk_weights": {
                    "mem_delete": 0.9,
                    "mem_write": 0.4,
                    "default": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )
    return p


def test_review_empty_queue(storage: SQLiteStorage) -> None:
    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1"])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    assert "No pending reviews for run 'run_1'." in out.getvalue()


def test_review_table_output_ranked(storage: SQLiteStorage, policy_path: Path) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    queue = ReviewQueue(storage, policy=policy)
    item_parked = queue.enqueue("run_1", "mem_write")  # low/medium risk (parked)
    item_imm = queue.enqueue("run_1", "mem_delete")  # high risk (immediate blocker)

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1", "--policy", str(policy_path)])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    output = out.getvalue()
    assert "Pending reviews for run 'run_1' (2 items):" in output
    assert "mem_delete" in output
    assert "mem_write" in output
    assert item_imm.review_id in output
    assert item_parked.review_id in output
    # Immediate item appears before parked item in table
    imm_pos = output.find("mem_delete")
    park_pos = output.find("mem_write")
    assert imm_pos < park_pos


def test_review_json_output(storage: SQLiteStorage) -> None:
    queue = ReviewQueue(storage)
    item = queue.enqueue("run_1", "mem_write")

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1", "--json"])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    payload = json.loads(out.getvalue())
    assert payload["run_id"] == "run_1"
    assert payload["pending_count"] == 1
    assert payload["items"][0]["review_id"] == item.review_id


def test_review_approve_individual_item(storage: SQLiteStorage) -> None:
    queue = ReviewQueue(storage)
    item = queue.enqueue("run_1", "mem_delete")

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(
        ["review", "run_1", "--approve", item.review_id, "--reviewer", "operator_1"]
    )

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    assert (
        f"Approved review item '{item.review_id}' for run 'run_1' (reviewer: operator_1)."
        in out.getvalue()
    )

    # Verify queue is now empty
    pending = queue.list_pending("run_1")
    assert len(pending) == 0


def test_review_approve_individual_item_json(storage: SQLiteStorage) -> None:
    queue = ReviewQueue(storage)
    item = queue.enqueue("run_1", "mem_delete")

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1", "--approve", item.review_id, "--json"])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    payload = json.loads(out.getvalue())
    assert payload["run_id"] == "run_1"
    assert payload["status"] == "approved"
    assert payload["review_id"] == item.review_id


def test_review_approve_missing_item_returns_not_found(storage: SQLiteStorage) -> None:
    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1", "--approve", "rev_missing"])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.NOT_FOUND
    assert "error: review item 'rev_missing' not found for run 'run_1'" in err.getvalue()


def test_review_approve_low_risk_bulk(storage: SQLiteStorage, policy_path: Path) -> None:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    queue = ReviewQueue(storage, policy=policy)
    queue.enqueue("run_1", "mem_write")  # risk 0.4 (parked)
    it_high = queue.enqueue("run_1", "mem_delete")  # risk 0.9 (immediate blocker)

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(
        ["review", "run_1", "--approve-low-risk", "--policy", str(policy_path)]
    )

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    assert "Approved 1 low-risk review items for run 'run_1'" in out.getvalue()

    # Only immediate item remains
    pending = queue.list_pending("run_1")
    assert len(pending) == 1
    assert pending[0].review_id == it_high.review_id


def test_review_approve_low_risk_bulk_json(storage: SQLiteStorage) -> None:
    queue = ReviewQueue(storage)
    queue.enqueue("run_1", "mem_write")

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1", "--approve-low-risk", "--json"])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.OK
    payload = json.loads(out.getvalue())
    assert payload["run_id"] == "run_1"
    assert payload["approved_count"] == 1


def test_review_missing_run_returns_not_found(storage: SQLiteStorage) -> None:
    from continuum.storage import RunNotFound

    parser = build_parser()
    args = parser.parse_args(["review", "missing_run"])

    with pytest.raises(RunNotFound):
        args.func(args, storage, io.StringIO(), io.StringIO())


def test_review_approve_already_approved_item_returns_error(storage: SQLiteStorage) -> None:
    queue = ReviewQueue(storage)
    item = queue.enqueue("run_1", "read_query")
    queue.approve("run_1", item.review_id, reviewer="operator")

    out = io.StringIO()
    err = io.StringIO()
    parser = build_parser()
    args = parser.parse_args(["review", "run_1", "--approve", item.review_id])

    code = args.func(args, storage, out, err)
    assert code == ExitCode.ERROR
    assert "already approved, nothing to approve" in err.getvalue()

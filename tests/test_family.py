"""Multi-agent hierarchies: parents, children, aggregated contracts (#243).

A child run is an ordinary run whose parent_run_id points at its supervisor.
The parent resume composes the family worst state: no RESUME while any
non-terminal child holds uncertainty or requires review. Siblings share
nothing mutable - coordination lives in the ledger and contracts.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from continuum.actions import ActionLedger
from continuum.actions.idempotency import idempotency_key
from continuum.cli import ExitCode, main
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "fam.db")


def test_start_refuses_an_unknown_parent(db: str) -> None:
    code, _, err = run("--db", db, "start", "kid", "--goal", "k", "--parent", "ghost")
    assert code == ExitCode.NOT_FOUND
    assert "does not exist" in err


def test_completed_parent_cannot_grow_children(db: str) -> None:
    run("--db", db, "start", "par", "--goal", "p")
    run("--db", db, "complete", "par", "--summary", "done")
    code, _, err = run("--db", db, "start", "kid", "--goal", "k", "--parent", "par")
    assert code == ExitCode.ERROR
    assert "completed" in err


def test_children_record_their_parent(db: str) -> None:
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    with SQLiteStorage(db) as store:
        assert store.get_run("kid").parent_run_id == "par"
        assert store.get_run("par").parent_run_id is None


def test_a2a_task_id_lands_in_metadata(db: str) -> None:
    run("--db", db, "start", "worker", "--goal", "w", "--a2a-task", "a2a-task-42")
    with SQLiteStorage(db) as store:
        meta = store.get_run("worker").metadata
    assert meta.get("a2a_task_id") == "a2a-task-42"


def test_tree_lists_children(db: str) -> None:
    run("--db", db, "start", "boss", "--goal", "supervise")
    run("--db", db, "start", "kid_ok", "--goal", "fine", "--parent", "boss")
    code, out, _ = run("--db", db, "tree", "boss")
    assert code == ExitCode.OK
    assert "boss" in out and "kid_ok" in out


def _family(db: str, count: int = 3) -> None:
    run("--db", db, "start", "boss", "--goal", "supervise")
    for i in range(count):
        run("--db", db, "start", f"kid{i}", "--goal", f"work {i}", "--parent", "boss")


def test_tree_limit_truncates_and_says_how_much_it_hid(db: str) -> None:
    """`--limit` used to be accepted and ignored (issue #321).

    Truncating silently is the failure mode that matters here: a reader cannot
    tell a two-child family from the first two children of a ten-child one, so
    the hidden count is printed with the flag that caused it.
    """
    _family(db, 3)
    code, out, err = run("--db", db, "tree", "boss", "--limit", "2")
    assert code == ExitCode.OK, err
    shown = [line for line in out.splitlines() if "kid" in line and "hidden" not in line]
    assert len(shown) == 2
    assert "1 of 3 children hidden by --limit 2" in out


def test_tree_without_limit_shows_every_child_and_hides_nothing(db: str) -> None:
    _family(db, 3)
    code, out, err = run("--db", db, "--json", "tree", "boss")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert len(payload["children"]) == 3
    assert payload["children_total"] == 3
    assert payload["children_hidden"] == 0
    assert "hidden by --limit" not in out


def test_tree_limit_reports_the_full_total_in_json(db: str) -> None:
    """A truncated `children` list stays honest about the family size, so a
    script reading the JSON can notice it is not looking at everything."""
    _family(db, 3)
    code, out, err = run("--db", db, "--json", "tree", "boss", "--limit", "1")
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    assert len(payload["children"]) == 1
    assert payload["children_total"] == 3
    assert payload["children_hidden"] == 2


@pytest.mark.parametrize("bad", ("0", "-1"))
def test_tree_refuses_a_limit_below_one(db: str, bad: str) -> None:
    """`--limit 0` would render a family with children as childless, which is
    the one output this command must never produce; refuse instead of clamp."""
    _family(db, 2)
    code, out, err = run("--db", db, "tree", "boss", "--limit", bad)
    assert code == ExitCode.ERROR
    assert "--limit must be 1 or more" in err
    assert "kid" not in out


def test_tree_limit_cannot_hide_a_child_from_family_safety(db: str) -> None:
    """Display-only truncation, deliberately not the change the issue proposed:
    limiting `children_of` would also truncate the input to the family roll-up,
    so an uncertain child could scroll off the list and leave `resume` calling a
    blocked family safe. The tree may omit it; the safety decision may not."""
    run("--db", db, "start", "boss", "--goal", "supervise")
    run("--db", db, "start", "kid_uncertain", "--goal", "risky", "--parent", "boss")
    run("--db", db, "start", "kid_late", "--goal", "later", "--parent", "boss")
    ActionLedger(SQLiteStorage(db), "kid_uncertain").claim("send_invoice", {}, key="invoice:I-9")

    code, out, err = run("--db", db, "--json", "tree", "boss", "--limit", "1")
    assert code == ExitCode.OK, err
    listed = {c["run_id"] for c in json.loads(out)["children"]}
    assert len(listed) == 1

    code, out, _ = run("--db", db, "--json", "resume", "boss")
    payload = json.loads(out)
    assert payload["mode"] == "request_human"
    assert any("kid_uncertain" in r for r in payload.get("family_rationale", []))


def test_tree_limit_is_documented_not_suppressed(capsys: pytest.CaptureFixture[str]) -> None:
    """The flag existed but `--help` hid it, which is what made it look absent
    rather than broken (issue #321)."""
    with pytest.raises(SystemExit) as exit_info:
        main(["tree", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--limit" in help_text
    assert "children" in help_text


def test_uncertain_child_blocks_the_parent_resume(db: str) -> None:
    """The acceptance core: parent alone would RESUME; an uncertain child
    forces request_human. Settling the child unblocks the family."""
    run("--db", db, "start", "par", "--goal", "supervise")
    run("--db", db, "start", "kid", "--goal", "work", "--parent", "par")
    ledger = ActionLedger(SQLiteStorage(db), "kid")
    ledger.claim("send_invoice", {}, key="invoice:I-9")

    code, out, _ = run("--db", db, "--json", "resume", "par")
    payload = json.loads(out)
    assert payload["mode"] == "request_human"
    assert any("kid" in r for r in payload.get("family_rationale", []))

    key = idempotency_key("send_invoice", None, scope="kid", key="invoice:I-9")
    ActionLedger(SQLiteStorage(db), "kid").reconcile(str(key), occurred=True)
    code, out, _ = run("--db", db, "--json", "resume", "par")
    payload = json.loads(out)
    assert payload["safe"] is True


def test_clean_children_do_not_block_the_parent(db: str) -> None:
    run("--db", db, "start", "par", "--goal", "supervise")
    code, out, _ = run("--db", db, "--json", "resume", "par")
    payload = json.loads(out)
    assert payload["safe"] is True

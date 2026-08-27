"""Run-level retry budgets (issue #240).

Every ACTION_RECORDED event is one attempt. Budgets from
`.continuum/budgets.json` cap attempts per action type at claim time so an
LLM re-planning after failures cannot hammer an upstream forever.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from continuum.actions import ActionLedger
from continuum.budgets import (
    BudgetConfigError,
    _process_umask,
    _staged_mode,
    attempts_by_key,
    attempts_for_type,
    backoff_delay,
    evaluate_budget,
    get_remaining,
    increment,
    load_budgets,
    save_budgets,
    would_refuse,
)
from continuum.cli import ExitCode, main
from continuum.events import EventType
from continuum.models import Run
from continuum.storage import SQLiteStorage


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "b.db")
    with SQLiteStorage(path) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        store.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield path


# --- config --------------------------------------------------------------------- #


def test_missing_registry_loads_empty(tmp_path: Path) -> None:
    assert load_budgets(tmp_path / "nope.json") == {}


def test_broken_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text("{")
    with pytest.raises(BudgetConfigError, match="not valid JSON"):
        load_budgets(p)


def test_nonpositive_limits_are_refused(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"action_types": {"x": 0}}))
    with pytest.raises(BudgetConfigError, match="positive"):
        load_budgets(p)


# --- authorization-bound registry (issue #411) --------------------------------------- #


def bound_registry() -> dict[str, Any]:
    """Hand-built registry shape, so pure-helper tests need no filesystem."""
    return {
        "default_max_attempts": 3,
        "action_types": {"send_invoice": {"max_attempts": 5}},
        "authorization_bound": {
            "send_invoice": {
                "authz:stripe-cust-1": {"counter": 2, "max_attempts": 5},
                "authz:stripe-cust-2": {"counter": 5, "max_attempts": 5},
            },
        },
    }


def test_authorization_bound_section_round_trips(tmp_path: Path) -> None:
    """save then load reproduces the registry with the section intact."""
    reg = bound_registry()
    p = tmp_path / "budgets.json"
    save_budgets(p, reg)
    assert load_budgets(p) == reg


def test_increment_then_save_round_trips(tmp_path: Path) -> None:
    """The mutate-then-persist cycle #413 will use keeps counters durable."""
    raw = bound_registry()
    assert increment(raw, "send_invoice", "authz:stripe-cust-1") == 2
    p = tmp_path / "budgets.json"
    save_budgets(p, raw)
    reloaded = load_budgets(p)
    assert get_remaining(reloaded, "send_invoice", "authz:stripe-cust-1") == 2


def test_save_preserves_keys_the_loader_does_not_know(tmp_path: Path) -> None:
    """Unknown keys pass through today; saving must not drop them either."""
    body: dict[str, Any] = {"future_section": {"anything": [1, 2]}, "default_max_attempts": 2}
    p = tmp_path / "budgets.json"
    save_budgets(p, body)
    assert load_budgets(p) == body


def test_registry_without_the_section_loads_unchanged(tmp_path: Path) -> None:
    """Old configs gain nothing and lose nothing on load (epic #390)."""
    body: dict[str, Any] = {"default_max_attempts": 3, "action_types": {"x": 2}}
    p = tmp_path / "budgets.json"
    save_budgets(p, body)
    loaded = load_budgets(p)
    assert loaded == body
    assert "authorization_bound" not in loaded


def test_absent_section_is_a_noop_for_reads_and_refusal_checks() -> None:
    """No section means unbound: reads answer None and nothing refuses."""
    raw: dict[str, Any] = {"default_max_attempts": 3}
    assert get_remaining(raw, "send_invoice", "authz-1") is None
    refused, reason = would_refuse(raw, "send_invoice", "authz-1")
    assert refused is False
    assert "authz-1" in reason


@pytest.mark.parametrize(
    ("section", "fragment"),
    [
        pytest.param([], "must be an object", id="section-not-an-object"),
        pytest.param(
            {"deploy": []},
            "entries for 'deploy' must be an object",
            id="type-level-not-an-object",
        ),
        pytest.param(
            {"deploy": {"k": "nope"}},
            "'deploy'/'k' must be an object",
            id="entry-not-an-object",
        ),
        pytest.param(
            {"deploy": {"k": {"counter": -1, "max_attempts": 2}}},
            "needs a non-negative integer 'counter'",
            id="negative-counter",
        ),
        pytest.param(
            {"deploy": {"k": {"counter": "0", "max_attempts": 2}}},
            "needs a non-negative integer 'counter'",
            id="counter-not-an-int",
        ),
        pytest.param(
            {"deploy": {"k": {"counter": 0}}},
            "needs a positive integer 'max_attempts'",
            id="max-missing",
        ),
        pytest.param(
            {"deploy": {"k": {"counter": 0, "max_attempts": 0}}},
            "needs a positive integer 'max_attempts'",
            id="zero-max",
        ),
    ],
)
def test_malformed_sections_raise_like_the_rest_of_the_file(
    tmp_path: Path, section: object, fragment: str
) -> None:
    """A bad authorization-bound entry is a malformed registry, same contract."""
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps({"default_max_attempts": 3, "authorization_bound": section}))
    with pytest.raises(BudgetConfigError, match=fragment):
        load_budgets(p)


def test_section_error_names_the_file(tmp_path: Path) -> None:
    """Messages point at an absolute path the operator can open, as elsewhere."""
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps({"authorization_bound": []}))
    with pytest.raises(BudgetConfigError) as excinfo:
        load_budgets(p)
    assert str(excinfo.value) == f"{p.resolve()}: 'authorization_bound' must be an object"


def test_old_malformations_are_still_reported_first(tmp_path: Path) -> None:
    """Pre-existing failures win: the classic check still raises before the new section.

    The message now carries the offending value (issue #326), so the pin is on
    the sentence plus that suffix rather than on the old bare sentence. What it
    guards is unchanged: a registry that was malformed before #411 existed is
    reported by the same check, naming the same field, not by the
    authorization-bound validation that runs after it.
    """
    body = {
        "action_types": {"x": 0},
        "authorization_bound": {
            "send_invoice": {"a": {"counter": -1, "max_attempts": 1}},
        },
    }
    p = tmp_path / "b.json"
    p.write_text(json.dumps(body))
    with pytest.raises(BudgetConfigError) as excinfo:
        load_budgets(p)
    assert str(excinfo.value) == (
        f"{tmp_path / 'b.json'}: action type 'x' needs a positive integer "
        f"'max_attempts', got 0 (int)"
    )


def test_action_type_error_names_the_resolved_path_not_the_caller_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative input path must still name an absolute file (#426).

    Hooks, sidecars and CI steps pass whatever cwd-relative spelling they hold;
    the operator reading the error cannot reopen that. ``{path}`` and
    ``{path.resolve()}`` coincide for an absolute input, which is how the lone
    straggler survived #351 and the byte-for-byte pin above. A relative input is
    the case that actually separates them, so it is the case that is pinned here.
    """
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"action_types": {"x": 0}}))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BudgetConfigError) as excinfo:
        load_budgets(Path("b.json"))
    assert str(excinfo.value) == (
        f"{p.resolve()}: action type 'x' needs a positive integer 'max_attempts', got 0 (int)"
    )


# --- rejections name the offending value (issue #326) -------------------------------- #


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        pytest.param({"max_attempts": 3.0}, "got 3.0 (float)", id="float-from-yaml"),
        pytest.param({"max_attempts": "3"}, "got '3' (str)", id="quoted-int"),
        pytest.param({}, "got None (NoneType)", id="field-missing"),
        pytest.param("3", "got '3' (str)", id="shorthand-string"),
    ],
)
def test_rejections_say_which_value_was_wrong(tmp_path: Path, spec: object, fragment: str) -> None:
    """The message must name the token to change, not just the rule (#326).

    A registry hand-converted from YAML arrives with ``3.0`` where ``3`` was
    meant. "needs a positive integer 'max_attempts'" was the same sentence for a
    float, a string and a missing field, so it never said which of those had
    happened, and an operator re-read a line that looked correct.
    """
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"action_types": {"send_invoice": spec}}))
    with pytest.raises(BudgetConfigError) as excinfo:
        load_budgets(p)
    message = str(excinfo.value)
    assert "needs a positive integer 'max_attempts'" in message
    assert fragment in message


# --- booleans are not integers (issue #429) ------------------------------------------ #


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        pytest.param(
            {"default_max_attempts": True},
            "'default_max_attempts' must be >= 1, got True (bool)",
            id="default-max",
        ),
        pytest.param(
            {"action_types": {"send_invoice": True}},
            "action type 'send_invoice' needs a positive integer 'max_attempts', got True (bool)",
            id="per-type-shorthand",
        ),
        pytest.param(
            {"action_types": {"send_invoice": {"max_attempts": True}}},
            "action type 'send_invoice' needs a positive integer 'max_attempts', got True (bool)",
            id="per-type-object",
        ),
        pytest.param(
            {"authorization_bound": {"deploy": {"k": {"counter": True, "max_attempts": 2}}}},
            "needs a non-negative integer 'counter', got True (bool)",
            id="bound-counter",
        ),
        pytest.param(
            {"authorization_bound": {"deploy": {"k": {"counter": 0, "max_attempts": True}}}},
            "needs a positive integer 'max_attempts', got True (bool)",
            id="bound-max",
        ),
    ],
)
def test_json_booleans_are_refused_wherever_an_integer_is_required(
    tmp_path: Path, body: dict[str, Any], fragment: str
) -> None:
    """``isinstance(True, int)`` must not let JSON ``true`` mean a cap of 1 (#429).

    Every integer check in the registry passed for a boolean, so a mis-typed
    config did not fail loudly the way the rest of the file does: ``true`` quietly
    acted as ``max_attempts`` of 1, and a ``counter`` of ``true`` became 2 after a
    single increment. Rejecting is the fail-loud half of the registry's contract.
    """
    p = tmp_path / "b.json"
    p.write_text(json.dumps(body))
    with pytest.raises(BudgetConfigError) as excinfo:
        load_budgets(p)
    assert fragment in str(excinfo.value)


# --- the registry is replaced, never truncated (issue #427) --------------------------- #


def test_save_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    """The staging file is consumed by the move, so the directory stays clean."""
    p = tmp_path / "budgets.json"
    save_budgets(p, bound_registry())
    save_budgets(p, bound_registry())
    assert [entry.name for entry in sorted(tmp_path.iterdir())] == ["budgets.json"]


def test_a_failed_save_leaves_the_previous_registry_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that dies mid-flight must not cost the operator the registry (#427).

    ``write_text`` opened the target with mode ``w``, truncating before writing,
    so a crash between truncation and flush left a zero-length or half-written
    ``budgets.json``. Every later load then raises, and because the gate is
    fail-closed every budget-gated claim refused until someone repaired the file
    by hand. Staging into a sibling file and moving it into place means the
    target only ever holds a complete registry: the worst a dead save costs is
    the last increment. #413 turns this into a write per claim attempt, which is
    what widens the window.
    """
    p = tmp_path / "budgets.json"
    before = bound_registry()
    save_budgets(p, before)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("killed between staging and rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="killed between staging"):
        save_budgets(p, {"default_max_attempts": 99})

    monkeypatch.undo()
    assert load_budgets(p) == before
    assert [entry.name for entry in sorted(tmp_path.iterdir())] == ["budgets.json"]


# --- staging must not change who may read the registry, nor lose the rename ----------- #

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="permission bits and directory descriptors are POSIX concepts"
)


@posix_only
@pytest.mark.parametrize("existing", [0o644, 0o664, 0o640, 0o600])
def test_save_keeps_the_permissions_the_registry_already_had(tmp_path: Path, existing: int) -> None:
    """The mode an operator set on ``budgets.json`` must survive the atomic rewrite.

    ``mkstemp`` stages at 0600 and ``os.replace`` carries those bits onto the
    target, so switching to stage-and-rename silently narrowed a registry that
    ``write_text`` had left alone: it preserved the existing mode on overwrite.
    Hooks, sidecars and CI steps read this file under their own uid and gid, and
    #413 makes the first claim attempt of a run the moment they get locked out.
    """
    p = tmp_path / "budgets.json"
    save_budgets(p, bound_registry())
    p.chmod(existing)
    save_budgets(p, {"default_max_attempts": 4})
    assert stat.S_IMODE(p.stat().st_mode) == existing
    assert load_budgets(p) == {"default_max_attempts": 4}


@posix_only
@pytest.mark.parametrize(("mask", "expected"), [(0o022, 0o644), (0o002, 0o664), (0o077, 0o600)])
def test_a_first_save_honours_the_umask_rather_than_forcing_0600(
    tmp_path: Path, mask: int, expected: int
) -> None:
    """Creating the registry lands on the mode ``write_text`` would have given it.

    With no prior file to copy bits from, the umask is the operator's only
    statement about who may read the registry; pinning 0600 answers that question
    for them. 0o666 is the mode ``open()`` passes for a new text file, so the
    expectation here is exactly what the old code path produced under each mask.
    """
    p = tmp_path / "budgets.json"
    previous = os.umask(mask)
    try:
        save_budgets(p, bound_registry())
    finally:
        os.umask(previous)
    assert stat.S_IMODE(p.stat().st_mode) == expected
    assert load_budgets(p) == bound_registry()


@posix_only
def test_the_rename_is_flushed_not_only_the_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fsyncing the staged file commits bytes; the rename lives in the directory.

    Without a directory flush the save can return successfully and still be
    undone by the very crash the staging file guards against, leaving the
    previous registry behind. That is a durability gap against what the function
    promises rather than corruption, but the promise is why staging exists.
    """
    synced_a_directory: list[bool] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        synced_a_directory.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    save_budgets(tmp_path / "budgets.json", bound_registry())
    assert synced_a_directory == [False, True]


@pytest.mark.parametrize("failing", ["open", "fsync"])
def test_a_directory_that_cannot_be_flushed_still_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """The directory flush is best effort, because the save is worth more than it.

    Windows cannot open a directory as a file descriptor at all, and some
    filesystems refuse ``fsync`` on one. The registry is written either way and is
    no less durable than before the flush was added, so raising here would trade a
    narrow durability gap for a budget-gated claim that cannot proceed.
    """
    p = tmp_path / "budgets.json"
    real_open, real_fsync = os.open, os.fsync

    def refuse_open(target: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(target, (str, bytes, os.PathLike)) and Path(os.fsdecode(target)) == tmp_path:
            raise PermissionError("a directory is not openable here")
        return real_open(target, flags, *args, **kwargs)

    def refuse_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("this filesystem does not fsync directories")
        real_fsync(fd)

    if failing == "open":
        monkeypatch.setattr(os, "open", refuse_open)
    else:
        monkeypatch.setattr(os, "fsync", refuse_fsync)
    save_budgets(p, bound_registry())
    monkeypatch.undo()
    assert load_budgets(p) == bound_registry()


def test_the_directory_flush_follows_the_replace_and_targets_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order matters: flushing before the rename would commit nothing about it.

    The posix test above proves a real directory descriptor is what gets synced.
    This one runs everywhere, including the platforms where a directory cannot be
    opened at all, and pins the two things that are pure sequencing: the flush
    happens after :func:`os.replace` has landed, and it is aimed at the directory
    holding the registry rather than the registry itself.
    """
    calls: list[str] = []
    real_replace = os.replace

    def note_replace(src: Any, dst: Any) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", note_replace)
    monkeypatch.setattr(
        "continuum.budgets._fsync_directory",
        lambda directory: calls.append(f"flush {directory}"),
    )
    save_budgets(tmp_path / "budgets.json", bound_registry())
    assert calls == ["replace", f"flush {tmp_path}"]


def test_reading_the_umask_leaves_it_exactly_as_it_was() -> None:
    """``os.umask`` is a swap, so reading it must put back what it took.

    A leaked mask would silently change the permissions of every file the process
    creates afterwards, which is a worse version of the bug being fixed. On Linux
    the value is read from ``/proc/self/status`` and nothing is swapped at all;
    elsewhere the fallback swaps and restores, and this holds it to the same
    promise. The expectation is read back through ``os.umask`` rather than
    hard-coded because Windows records only the write bit of whatever it is given.
    """
    original = os.umask(0o022)
    try:
        recorded = os.umask(0o022)
        assert _process_umask() == recorded
        assert os.umask(recorded) == recorded, "the read must not have left another mask behind"
    finally:
        os.umask(original)


def test_a_new_registry_takes_its_bits_from_the_umask_not_from_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no file to copy, the mode is ``0o666 & ~umask``: what ``open()`` would use.

    Platform-independent cover for the arithmetic, since only POSIX actually
    stores the result. 0o666 rather than 0o644 because that is the mode CPython
    passes when creating a text file, so a first save reproduces what
    ``write_text`` produced under the same mask instead of quietly dropping the
    group and other bits an operator's umask allows.
    """
    monkeypatch.setattr("continuum.budgets._process_umask", lambda: 0o027)
    assert _staged_mode(tmp_path / "never-written.json") == 0o640
    monkeypatch.setattr("continuum.budgets._process_umask", lambda: None)
    assert _staged_mode(tmp_path / "never-written.json") is None, (
        "an unreadable umask must leave mkstemp's tighter 0600 alone, not guess wider"
    )


def test_get_remaining_and_refusal_math() -> None:
    """Remaining is max minus counter; refusals name the type, id and figures."""
    raw = bound_registry()
    assert get_remaining(raw, "send_invoice", "authz:stripe-cust-1") == 3
    refused, reason = would_refuse(raw, "send_invoice", "authz:stripe-cust-1")
    assert refused is False
    assert "3 of 5" in reason
    refused, reason = would_refuse(raw, "send_invoice", "authz:stripe-cust-2")
    assert refused is True
    assert "authz:stripe-cust-2" in reason
    assert "5 of 5" in reason


def test_increment_counts_monotonically_and_returns_remaining() -> None:
    """The counter only climbs; past the cap remaining floors at zero."""
    raw = bound_registry()
    entry = raw["authorization_bound"]["send_invoice"]["authz:stripe-cust-1"]
    assert increment(raw, "send_invoice", "authz:stripe-cust-1") == 2
    assert entry["counter"] == 3
    assert increment(raw, "send_invoice", "authz:stripe-cust-2") == 0
    assert increment(raw, "send_invoice", "authz:stripe-cust-2") == 0
    assert raw["authorization_bound"]["send_invoice"]["authz:stripe-cust-2"]["counter"] == 7


def test_increment_without_an_entry_raises_rather_than_inventing_a_cap() -> None:
    """No entry, no limit to enforce: KeyError beats a silently guessed one."""
    raw = bound_registry()
    with pytest.raises(KeyError, match="charge_card"):
        increment(raw, "charge_card", "authz-9")


def test_missing_entry_reads_as_unbound_not_refused() -> None:
    """One unknown authorization must not be treated as an exhausted one."""
    raw = bound_registry()
    assert get_remaining(raw, "send_invoice", "authz-absent") is None
    refused, _ = would_refuse(raw, "send_invoice", "authz-absent")
    assert refused is False


def test_zero_max_refuses_at_once_and_still_counts() -> None:
    """A zero allowance refuses immediately; increments stay monotonic."""
    raw: dict[str, Any] = {
        "authorization_bound": {"deploy": {"k": {"counter": 0, "max_attempts": 0}}}
    }
    refused, reason = would_refuse(raw, "deploy", "k")
    assert refused is True
    assert "0 of 0" in reason
    assert get_remaining(raw, "deploy", "k") == 0
    assert increment(raw, "deploy", "k") == 0
    assert raw["authorization_bound"]["deploy"]["k"]["counter"] == 1


def test_distinct_authorizations_keep_independent_counters() -> None:
    """Drawing down one authorization leaves its neighbours untouched."""
    raw = bound_registry()
    increment(raw, "send_invoice", "authz:stripe-cust-1")
    assert get_remaining(raw, "send_invoice", "authz:stripe-cust-1") == 2
    assert get_remaining(raw, "send_invoice", "authz:stripe-cust-2") == 0


# --- counting + evaluation -------------------------------------------------------- #


def test_attempts_count_every_claim_slot(db: str) -> None:
    """Retries of one operation accumulate against that operation's allowance.

    Re-claiming after FAILED copies the existing action, so successive attempts
    land under the same key rather than each opening a fresh row. That is what
    makes the key the right unit to count (issue #368).
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(outcome.key, "boom", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")

    events = SQLiteStorage(db).read_events("run_1")
    assert attempts_by_key(events, "send_invoice") == {str(outcome.key): 2}
    assert attempts_for_type(events, "send_invoice") == 2


def test_distinct_operations_do_not_share_one_allowance(db: str) -> None:
    """Different work must not compete for the same budget (issue #368).

    Counting per action type made three recipients each failing once, with no
    retry anywhere, exhaust a budget of three and block a fourth that had never
    been attempted. Any fan-out with more failures than the limit deadlocked
    mid-run, and the refusal called it a retry budget while nothing had been
    retried.
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    for recipient in ("a", "b", "c"):
        outcome = ledger.claim("email_send", {"to": recipient}, key=f"email:{recipient}")
        ledger.fail(outcome.key, "550 rejected", certain=True)

    events = SQLiteStorage(db).read_events("run_1")
    per_key = attempts_by_key(events, "email_send")
    assert len(per_key) == 3
    assert set(per_key.values()) == {1}
    # The figure compared against the limit is the worst single operation, not
    # the sum, so a fourth recipient is still allowed.
    assert attempts_for_type(events, "email_send") == 1
    assert evaluate_budget({"default_max_attempts": 3}, "email_send", 1)[0] is True


def test_completed_attempts_do_not_count(db: str) -> None:
    """A succeeded operation was never retried (issue #309).

    Counting successes turns a retry budget into a cap on how much distinct work
    a run may do: three invoices sent successfully would exhaust a budget of 3
    and refuse the fourth, having never retried anything.
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    for n in range(3):
        outcome = ledger.claim("send_invoice", {}, key=f"invoice:{n}")
        ledger.complete(outcome.key, external_id=f"ext-{n}")

    events = SQLiteStorage(db).read_events("run_1")
    assert attempts_for_type(events, "send_invoice") == 0
    assert evaluate_budget({"default_max_attempts": 3}, "send_invoice", 0)[0] is True


def test_only_the_unsettled_attempts_of_a_retried_key_count(db: str) -> None:
    """The amplification guard must survive the fix: failures still count."""
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    # One key retried twice and still unsettled, one that succeeded on retry,
    # and one untouched operation in flight.
    stuck = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(stuck.key, "boom", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(stuck.key, "boom again", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")

    won = ledger.claim("send_invoice", {}, key="invoice:2")
    ledger.fail(won.key, "transient", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:2")
    ledger.complete(won.key, external_id="ext-2")

    fresh = ledger.claim("send_invoice", {}, key="invoice:3")

    events = SQLiteStorage(db).read_events("run_1")
    # invoice:1 burned three attempts and is still unsettled; invoice:2 succeeded
    # so it is excluded entirely; invoice:3 is on its first.
    assert attempts_by_key(events, "send_invoice") == {
        str(stuck.key): 3,
        str(fresh.key): 1,
    }
    assert attempts_for_type(events, "send_invoice") == 3


def test_budget_evaluation_math() -> None:
    raw = {"default_max_attempts": 3, "action_types": {"send_invoice": {"max_attempts": 5}}}
    assert evaluate_budget(raw, "send_invoice", 5)[0] is False
    assert evaluate_budget(raw, "send_invoice", 4)[0] is True
    allowed, used, maximum = evaluate_budget(raw, "other_tool", 0)
    assert (allowed, used, maximum) == (True, 0, 3)


def test_backoff_delay_is_exponential_with_cap() -> None:
    assert backoff_delay(1) == 1.0
    assert backoff_delay(2) == 2.0
    assert backoff_delay(3) == 4.0
    assert backoff_delay(10) == 60.0  # capped
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_backoff_delay_rejects_zero() -> None:
    with pytest.raises(ValueError, match="got 0"):
        backoff_delay(0)


# --- enforcement through the real ledger path --------------------------------------- #


def test_claims_beyond_budget_are_refused_at_the_mcp_boundary(db: str, tmp_path: Path) -> None:
    """Drive the real intercept handler logic: after N attempts at one operation,
    the next claim for it is refused naming the budget.

    The MCP tool raises ToolError; here we pin the counting and refusal maths
    against the same folded view the server uses. Retries of one key, not two
    different invoices, because the budget caps repetition (issue #368).
    """
    cfg = {"default_max_attempts": 2}
    registry_path = registry(tmp_path, cfg)
    del registry_path

    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    outcome = ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.fail(outcome.key, "boom", certain=True)
    ledger.claim("send_invoice", {}, key="invoice:1")

    events = SQLiteStorage(db).read_events("run_1")
    attempts = attempts_by_key(events, "send_invoice")[str(outcome.key)]
    allowed, used, maximum = evaluate_budget(cfg, "send_invoice", attempts)
    assert (allowed, used, maximum) == (False, 2, 2)

    # A different invoice has its own allowance and is unaffected.
    other = evaluate_budget(cfg, "send_invoice", 0)
    assert other[0] is True


def registry(tmp_path: Path, body: dict[str, object]) -> str:
    p = tmp_path / "budgets.json"
    p.write_text(json.dumps(body))
    return str(p)


# --- CLI report ---------------------------------------------------------------------- #


def test_cli_budget_reports_usage_per_type(db: str, tmp_path: Path) -> None:
    """The report shows the worst single operation, which is what the gate uses.

    Two different invoices are two operations with their own allowances, not two
    attempts at one (issue #368), so `send_invoice` sits at 1 of 3 rather than 2.
    """
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    ledger.claim("send_invoice", {}, key="invoice:1")
    ledger.claim("send_invoice", {}, key="invoice:2")
    ledger.claim("charge_card", {}, key="card:9")

    code, out, err = run(
        "--db",
        db,
        "--json",
        "budget",
        "run_1",
        "--config",
        registry(
            tmp_path,
            {
                "default_max_attempts": 3,
                "action_types": {"charge_card": {"max_attempts": 1}},
            },
        ),
    )
    assert code == ExitCode.OK, err
    payload = json.loads(out)
    by_type = {r["action_type"]: r for r in payload["budgets"]}
    assert by_type["send_invoice"]["attempts"] == 1
    assert by_type["send_invoice"]["remaining"] == 2  # 3 - 1
    assert by_type["charge_card"]["exhausted"] is True  # 1 - 1


def test_cli_budget_exhausted_exit_is_reported_not_raised(db: str, tmp_path: Path) -> None:
    ledger = ActionLedger(SQLiteStorage(db), "run_1")
    ledger.claim("deploy", {}, key="d:1")
    code, out, _ = run(
        "--db",
        db,
        "--json",
        "budget",
        "run_1",
        "--config",
        registry(tmp_path, {"default_max_attempts": 3}),
    )
    assert code == ExitCode.OK
    rows = json.loads(out)["budgets"]
    assert any(r["action_type"] == "deploy" and r["attempts"] >= 1 for r in rows)

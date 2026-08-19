from __future__ import annotations

from collections.abc import Iterator

import pytest

from continuum.actions import (
    ActionLedger,
    LedgerError,
    arguments_hash,
    idempotency_key,
)
from continuum.events import EventType
from continuum.models import Action, ActionStatus, Run, UnknownSideEffect
from continuum.storage import SQLiteStorage


@pytest.fixture
def store() -> Iterator[SQLiteStorage]:
    storage = SQLiteStorage(":memory:")
    storage.create_run(Run(run_id="run_1", goal="g"))
    storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
    yield storage
    storage.close()


@pytest.fixture
def ledger(store: SQLiteStorage) -> ActionLedger:
    return ActionLedger(store, "run_1")


ISSUE = {"title": "Bug report", "body": "It broke"}


# --- idempotency keys ------------------------------------------------------ #


def test_argument_order_does_not_change_the_key() -> None:
    a = idempotency_key("github.create_issue", {"title": "x", "body": "y"})
    b = idempotency_key("github.create_issue", {"body": "y", "title": "x"})
    assert a == b


def test_different_arguments_produce_different_keys() -> None:
    a = idempotency_key("github.create_issue", {"title": "x"})
    b = idempotency_key("github.create_issue", {"title": "y"})
    assert a != b


def test_different_action_types_produce_different_keys() -> None:
    assert idempotency_key("a.do", {"x": 1}) != idempotency_key("b.do", {"x": 1})


def test_scope_separates_runs() -> None:
    a = idempotency_key("send_email", {"to": "x"}, scope="run_1")
    b = idempotency_key("send_email", {"to": "x"}, scope="run_2")
    assert a != b
    assert idempotency_key("send_email", {"to": "x"}) != a


def test_volatile_fields_are_excluded_so_retries_deduplicate() -> None:
    """A retry counter must not make a retry look like a new action."""
    first = idempotency_key("call", {"payload": "p", "attempt": 1}, volatile=["attempt"])
    second = idempotency_key("call", {"payload": "p", "attempt": 2}, volatile=["attempt"])
    assert first == second

    without = idempotency_key("call", {"payload": "p", "attempt": 1})
    with_retry = idempotency_key("call", {"payload": "p", "attempt": 2})
    assert without != with_retry  # not excluded by default


def test_an_empty_action_type_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        idempotency_key("", {})


def test_unhashable_arguments_fail_loudly() -> None:
    """A key that varies between runs would silently disable deduplication."""
    with pytest.raises(TypeError):
        arguments_hash({"bad": {1, 2}})


# --- the basic protocol ---------------------------------------------------- #


def test_a_first_claim_is_fresh_and_recorded_as_started(ledger: ActionLedger) -> None:
    outcome = ledger.claim("github.create_issue", ISSUE)
    assert outcome.fresh
    assert outcome.action.status is ActionStatus.STARTED
    assert outcome.action.arguments_hash is not None


def test_completing_stores_the_external_id_and_result(ledger: ActionLedger) -> None:
    outcome = ledger.claim("github.create_issue", ISSUE)
    action = ledger.complete(outcome.key, external_id="481", result={"url": "/issues/481"})

    assert action.status is ActionStatus.COMPLETED
    assert action.external_id == "481"
    assert action.result_hash is not None
    assert action.completed_at is not None


def test_a_repeat_claim_returns_the_previous_result_instead_of_redoing_it(
    ledger: ActionLedger,
) -> None:
    """The headline behaviour: no duplicate GitHub issue."""
    first = ledger.claim("github.create_issue", ISSUE)
    ledger.complete(first.key, external_id="481", result={"url": "/issues/481"})

    second = ledger.claim("github.create_issue", ISSUE)
    assert not second.fresh
    assert second.already_completed
    assert second.external_id == "481"
    assert second.result == {"url": "/issues/481"}


def test_a_different_action_is_not_deduplicated(ledger: ActionLedger) -> None:
    first = ledger.claim("github.create_issue", ISSUE)
    ledger.complete(first.key, external_id="481")
    second = ledger.claim("github.create_issue", {"title": "Different", "body": "x"})
    assert second.fresh


def test_a_failed_action_may_be_retried(ledger: ActionLedger) -> None:
    first = ledger.claim("api.call", {"x": 1})
    ledger.fail(first.key, "500 from upstream")

    retry = ledger.claim("api.call", {"x": 1})
    assert retry.fresh
    assert retry.action.status is ActionStatus.STARTED


def test_a_compensated_action_may_be_performed_again(ledger: ActionLedger) -> None:
    first = ledger.claim("github.create_issue", ISSUE)
    ledger.complete(first.key, external_id="481")
    ledger.compensate(first.key, note="issue closed as duplicate", by="close_issue")

    again = ledger.claim("github.create_issue", ISSUE)
    assert again.fresh
    assert again.action.external_id is None  # the old effect is not reused


def test_completing_an_unknown_key_is_refused(ledger: ActionLedger) -> None:
    with pytest.raises(LedgerError, match="no action recorded"):
        ledger.complete("nonexistent", external_id="1")


# --- the crash gap: the reason this module exists -------------------------- #


def test_an_interrupted_action_refuses_to_silently_retry(ledger: ActionLedger) -> None:
    """Crash between claim and complete: the effect may or may not have landed."""
    ledger.claim("github.create_issue", ISSUE)  # never completed — process died

    with pytest.raises(UnknownSideEffect, match="may or may not have occurred"):
        ledger.claim("github.create_issue", ISSUE)


def test_an_interrupted_action_is_marked_uncertain_for_later(
    ledger: ActionLedger,
) -> None:
    ledger.claim("github.create_issue", ISSUE)
    with pytest.raises(UnknownSideEffect):
        ledger.claim("github.create_issue", ISSUE)

    uncertain = ledger.pending()
    assert len(uncertain) == 1
    assert uncertain[0].status is ActionStatus.UNKNOWN
    assert uncertain[0].side_effect_uncertain


def test_a_timeout_is_not_evidence_of_absence(ledger: ActionLedger) -> None:
    """A request that timed out may still have been processed."""
    outcome = ledger.claim("payment.charge", {"amount": 100})
    action = ledger.fail(outcome.key, "timeout after 30s", certain=False)

    assert action.status is ActionStatus.UNKNOWN
    assert action.side_effect_uncertain
    assert ledger.pending()

    with pytest.raises(UnknownSideEffect):
        ledger.claim("payment.charge", {"amount": 100})


def test_a_definite_failure_is_distinguished_from_a_timeout(
    ledger: ActionLedger,
) -> None:
    outcome = ledger.claim("payment.charge", {"amount": 100})
    action = ledger.fail(outcome.key, "400 invalid card", certain=True)

    assert action.status is ActionStatus.FAILED
    assert not action.side_effect_uncertain
    assert ledger.pending() == []


def test_an_inline_resolver_can_rescue_an_interrupted_action(
    ledger: ActionLedger,
) -> None:
    from continuum.actions.ledger import ActionOutcome

    ledger.claim("github.create_issue", ISSUE)

    def found_it(action: object) -> ActionOutcome:
        key = idempotency_key("github.create_issue", ISSUE, scope="run_1")
        recovered = ledger.reconcile(key, occurred=True, external_id="481")
        return ActionOutcome(key=key, action=recovered, fresh=False)

    outcome = ledger.claim("github.create_issue", ISSUE, on_unknown=found_it)
    assert not outcome.fresh
    assert outcome.external_id == "481"


def test_an_inline_resolution_settles_the_ledger_durably(
    ledger: ActionLedger, store: SQLiteStorage
) -> None:
    """Issue #45: a resolver that does not itself reconcile must still persist.

    Returning the resolution only to the caller left the stored action UNKNOWN,
    so the next claim re-raised, ``pending()`` never drained, and the recovery
    engine asked for a human forever.
    """
    from continuum.actions.ledger import ActionOutcome

    first = ledger.claim("act.x", {"k": 1})
    ledger.fail(first.key, "boom", certain=False)

    def resolve(existing: Action) -> ActionOutcome:
        settled = existing.model_copy(
            update={
                "status": ActionStatus.COMPLETED,
                "external_id": "ext-1",
                "result": {"ok": True},
                "side_effect_uncertain": False,
            }
        )
        return ActionOutcome(key=first.key, action=settled, fresh=False)

    resolved = ledger.claim("act.x", {"k": 1}, on_unknown=resolve)
    assert resolved.action.status is ActionStatus.COMPLETED

    # The durable state, not just the return value.
    assert ledger.get(first.key).status is ActionStatus.COMPLETED
    assert ledger.pending() == []

    # A fresh ledger replaying the same events must agree.
    replayed = ActionLedger(store, "run_1")
    assert replayed.get(first.key).status is ActionStatus.COMPLETED
    assert replayed.get(first.key).external_id == "ext-1"
    assert replayed.pending() == []


# --- reconciliation -------------------------------------------------------- #


def test_reconciling_as_occurred_prevents_any_repeat(ledger: ActionLedger) -> None:
    outcome = ledger.claim("github.create_issue", ISSUE)
    ledger.fail(outcome.key, "connection lost", certain=False)

    ledger.reconcile(outcome.key, occurred=True, external_id="481", note="found via search")

    repeat = ledger.claim("github.create_issue", ISSUE)
    assert not repeat.fresh
    assert repeat.external_id == "481"
    assert ledger.pending() == []


def test_reconciling_as_not_occurred_permits_a_retry(ledger: ActionLedger) -> None:
    outcome = ledger.claim("github.create_issue", ISSUE)
    ledger.fail(outcome.key, "connection lost", certain=False)

    ledger.reconcile(outcome.key, occurred=False, note="no matching issue found")

    retry = ledger.claim("github.create_issue", ISSUE)
    assert retry.fresh


def test_reconciling_as_not_occurred_clears_the_recorded_effect(
    ledger: ActionLedger,
) -> None:
    """Issue #29: deciding the effect never happened invalidates its evidence.

    Leaving ``external_id`` and ``result`` behind showed a downstream reader a
    stale external identity and a stale success payload on an action the system
    had just decided never completed.
    """
    outcome = ledger.claim("t.send", {"id": "row-1"})
    ledger.complete(outcome.key, external_id="EXT-9", result={"ok": True})

    ledger.reconcile(outcome.key, occurred=False, note="probe found nothing")

    settled = ledger.get(outcome.key)
    assert settled.status is ActionStatus.FAILED
    assert settled.external_id is None
    assert settled.result is None


def test_reconciliation_is_recorded_as_its_own_event(
    ledger: ActionLedger, store: SQLiteStorage
) -> None:
    outcome = ledger.claim("api.call", {})
    ledger.reconcile(outcome.key, occurred=True, external_id="x")

    kinds = [e.type for e in store.read_events("run_1")]
    assert EventType.ACTION_RECONCILED in kinds


def test_flagging_for_review_escalates(ledger: ActionLedger) -> None:
    outcome = ledger.claim("payment.charge", {"amount": 100})
    action = ledger.flag_for_review(outcome.key, "cannot verify with provider")
    assert action.status is ActionStatus.REQUIRES_REVIEW


# --- durability ------------------------------------------------------------ #


def test_the_ledger_is_rebuilt_from_events(store: SQLiteStorage) -> None:
    outcome = ActionLedger(store, "run_1").claim("github.create_issue", ISSUE)
    ActionLedger(store, "run_1").complete(outcome.key, external_id="481")

    rebuilt = ActionLedger(store, "run_1")
    assert rebuilt.get(outcome.key).external_id == "481"  # type: ignore[union-attr]


def test_the_ledger_survives_a_process_restart(tmp_path: object) -> None:
    from pathlib import Path

    db = Path(str(tmp_path)) / "agent.db"
    with SQLiteStorage(db) as store:
        store.create_run(Run(run_id="run_1", goal="g"))
        outcome = ActionLedger(store, "run_1").claim("github.create_issue", ISSUE)
        ActionLedger(store, "run_1").complete(outcome.key, external_id="481")

    with SQLiteStorage(db) as store:
        repeat = ActionLedger(store, "run_1").claim("github.create_issue", ISSUE)
        assert not repeat.fresh
        assert repeat.external_id == "481"


def test_ledgers_for_different_runs_are_isolated(store: SQLiteStorage) -> None:
    store.create_run(Run(run_id="run_2", goal="g"))
    first = ActionLedger(store, "run_1")
    second = ActionLedger(store, "run_2")

    outcome = first.claim("github.create_issue", ISSUE)
    first.complete(outcome.key, external_id="481")

    assert second.claim("github.create_issue", ISSUE).fresh


def test_a_globally_scoped_action_deduplicates_across_runs(
    store: SQLiteStorage,
) -> None:
    """Some effects must happen once ever, not once per run."""
    key_a = idempotency_key("send_welcome_email", {"to": "x@y.z"})
    key_b = idempotency_key("send_welcome_email", {"to": "x@y.z"})
    assert key_a == key_b

    ledger = ActionLedger(store, "run_1")
    outcome = ledger.claim("send_welcome_email", {"to": "x@y.z"}, scoped_to_run=False)
    ledger.complete(outcome.key, external_id="msg_1")
    assert not ledger.claim("send_welcome_email", {"to": "x@y.z"}, scoped_to_run=False).fresh


def test_a_resolver_that_declines_falls_back_to_refusing(
    ledger: ActionLedger,
) -> None:
    """A resolver returning None must not be read as 'safe to proceed'."""
    ledger.claim("github.create_issue", ISSUE)

    with pytest.raises(UnknownSideEffect):
        ledger.claim("github.create_issue", ISSUE, on_unknown=lambda action: None)


def test_a_malformed_action_event_is_skipped_not_fatal(
    ledger: ActionLedger, store: SQLiteStorage
) -> None:
    """A foreign writer's action event must not make the ledger unreadable."""
    store.append_event("run_1", EventType.ACTION_RECORDED, {"note": "no key here"})
    outcome = ledger.claim("github.create_issue", ISSUE)
    ledger.complete(outcome.key, external_id="481")

    assert len(ledger.all()) == 1
    assert not ledger.claim("github.create_issue", ISSUE).fresh


def test_all_and_pending_report_the_ledger_contents(ledger: ActionLedger) -> None:
    done = ledger.claim("a.do", {"n": 1})
    ledger.complete(done.key)
    ledger.claim("b.do", {"n": 2})  # left in flight

    assert len(ledger.all()) == 2
    assert [a.action_type for a in ledger.pending()] == ["b.do"]


def test_an_explicit_key_lets_a_repeat_be_a_genuine_second_action(
    ledger: ActionLedger,
) -> None:
    """Argument hashing cannot express "this repeat is intentional".

    Two identical reminders are two sends, not one. Without an explicit key the
    second is silently deduplicated away — failing closed, but still wrong.
    """
    args = {"to": "x@y.z", "body": "Standup in 5"}

    first = ledger.claim("send_reminder", args, key="reminder-monday")
    ledger.complete(first.key, external_id="msg_1")

    second = ledger.claim("send_reminder", args, key="reminder-tuesday")
    assert second.fresh, "a distinct key must not collide with an earlier send"

    repeat = ledger.claim("send_reminder", args, key="reminder-monday")
    assert not repeat.fresh
    assert repeat.external_id == "msg_1"


def test_an_explicit_key_ignores_argument_drift(ledger: ActionLedger) -> None:
    """The caller's key is the identity; incidental argument changes are not."""
    first = ledger.claim("charge", {"amount": 100, "attempt": 1}, key="order-42")
    ledger.complete(first.key, external_id="ch_1")

    retry = ledger.claim("charge", {"amount": 100, "attempt": 2}, key="order-42")
    assert not retry.fresh
    assert retry.external_id == "ch_1"


def test_an_empty_explicit_key_is_refused(ledger: ActionLedger) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ledger.claim("send_reminder", {"to": "x"}, key="")


# --- defensive identity fallback (issue #6) -------------------------------- #
#
# These mirror the real drift observed across three Claude Code e2e runs: the
# idempotency key hashes arguments verbatim, so an agent that renames an
# argument field (`target` vs `outbox_file` vs `outfile`) or reformats a path
# between sessions computes a different key. The only stable identity across
# every shape was the invoice id token (e.g. `INV-001`), which survives as a
# scalar value, a path basename, and an external_id stem.


def test_path_canonicalization_unifies_equivalent_spellings() -> None:
    """A path normalized lexically must hash the same as its canonical form."""
    assert arguments_hash({"outbox": "/tmp/e2e-outbox/./INV-001.sent"}) == arguments_hash(
        {"outbox": "/tmp/e2e-outbox/INV-001.sent"}
    )
    assert arguments_hash({"outbox": "/tmp//e2e-outbox/INV-001.sent"}) == arguments_hash(
        {"outbox": "/tmp/e2e-outbox/INV-001.sent"}
    )


def test_path_canonicalization_does_not_collapse_distinct_paths() -> None:
    a = arguments_hash({"target": "/tmp/e2e-outbox/INV-001.sent"})
    b = arguments_hash({"target": "/tmp/e2e-outbox/INV-002.sent"})
    assert a != b


def test_identity_match_recognises_a_completed_action_across_field_renames(
    ledger: ActionLedger,
) -> None:
    """Session 1 uses invoice_id+target; session 2 uses invoice only."""
    first = ledger.claim(
        "send_invoice",
        {"invoice_id": "INV-001", "target": "/tmp/e2e-outbox/INV-001.sent"},
    )
    ledger.complete(first.key, external_id="INV-001.sent")

    second = ledger.claim("send_invoice", {"invoice": "INV-001"})
    assert not second.fresh
    assert second.already_completed
    assert second.external_id == "INV-001.sent"


def test_identity_match_survives_external_id_shape_drift(ledger: ActionLedger) -> None:
    """Session 1 completes with an absolute path; session 2 re-claims by id."""
    first = ledger.claim(
        "send_invoice",
        {"invoice_id": "INV-002", "outbox_file": "/tmp/e2e-outbox/INV-002.sent"},
    )
    ledger.complete(first.key, external_id="/tmp/e2e-outbox/INV-002.sent")

    again = ledger.claim("send_invoice", {"invoice_id": "INV-002"})
    assert not again.fresh
    assert again.external_id == "/tmp/e2e-outbox/INV-002.sent"


def test_identity_match_does_not_collapse_distinct_invoices(ledger: ActionLedger) -> None:
    """A different invoice id is different work, not a drift of the same work."""
    first = ledger.claim(
        "send_invoice",
        {"invoice_id": "INV-003", "target": "/tmp/e2e-outbox/INV-003.sent"},
    )
    ledger.complete(first.key, external_id="INV-003.sent")

    other = ledger.claim("send_invoice", {"invoice": "INV-004"})
    assert other.fresh
    assert other.action.arguments["invoice"] == "INV-004"


def test_identity_match_does_not_cross_action_types(ledger: ActionLedger) -> None:
    """send_invoice and send-invoice-email are different operations."""
    first = ledger.claim(
        "send_invoice",
        {"invoice_id": "INV-005", "target": "/tmp/e2e-outbox/INV-005.sent"},
    )
    ledger.complete(first.key, external_id="INV-005.sent")

    other = ledger.claim("send-invoice-email", {"invoice": "INV-005"})
    assert other.fresh


def test_identity_match_surfaces_an_interrupted_action(ledger: ActionLedger) -> None:
    """A drifted re-claim of an interrupted action must not open a fresh slot."""
    ledger.claim(
        "send_invoice",
        {"invoice_id": "INV-006", "target": "/tmp/e2e-outbox/INV-006.sent"},
    )

    with pytest.raises(UnknownSideEffect, match="may or may not have occurred"):
        ledger.claim("send_invoice", {"invoice": "INV-006"})

    uncertain = ledger.pending()
    assert len(uncertain) == 1
    assert uncertain[0].side_effect_uncertain


def test_identity_match_requires_a_distinctive_token(ledger: ActionLedger) -> None:
    """Generic values (counts, status words) must never trigger a match."""
    first = ledger.claim("api.call", {"invoice_id": "INV-007", "status": "sent"})
    ledger.complete(first.key, external_id="INV-007.sent")

    # "1" is too short to distinguish anything and "sent" is a weak token;
    # only INV-008 would be new work.
    other = ledger.claim("api.call", {"id": 1, "status": "sent"})
    assert other.fresh


def test_identity_match_recognises_a_plain_word_resource(ledger: ActionLedger) -> None:
    """Issue #33: a word like 'invoice' names a resource as well as 'INV-001'.

    Requiring a digit, '@', or '.' meant every plain-word identity was dropped,
    so drift across sessions opened a second slot and the effect ran twice.
    """
    first = ledger.claim("publish", {"topic": "invoice"})
    ledger.complete(first.key, external_id="published")

    again = ledger.claim("publish", {"subject": "invoice"})
    assert not again.fresh
    assert again.external_id == "published"


def test_identity_match_recognises_a_numeric_resource_id(ledger: ActionLedger) -> None:
    """Issue #36: a row id of 4821 identifies a row as well as INV-001 does.

    Numeric ids were dropped twice over -- as purely-numeric tokens, and because
    only ``str`` values were tokenised at all, so an ``int`` never became one.
    """
    first = ledger.claim("db.update", {"row_id": 4821})
    ledger.complete(first.key, external_id="updated")

    again = ledger.claim("db.update", {"id": 4821})
    assert not again.fresh
    assert again.external_id == "updated"

    other = ledger.claim("db.update", {"row_id": 9999})
    assert other.fresh, "a different row is different work"


def test_identity_match_survives_an_absolute_versus_relative_path(
    ledger: ActionLedger,
) -> None:
    """The same file rendered two ways is one action (the issue #6 scenario).

    Each spelling carries a container the other lacks, so identity is compared at
    the leaf: ``INV-5.pdf`` and its stem, not the directory that precedes them.
    CONTINUUM-Bench measures exactly this path, so a regression here shows up as
    duplicate side effects in the benchmark rather than as a unit failure.
    """
    first = ledger.claim("bench.send", {"file": "/data/invoices/INV-5.pdf", "invoice": "INV-5"})
    ledger.complete(first.key, external_id="ext-5", result={"ok": True})

    again = ledger.claim("bench.send", {"file": "invoices/INV-5.pdf", "invoice": "INV-5"})
    assert not again.fresh, "a re-rendered path is the same file, not new work"
    assert again.external_id == "ext-5"


def test_identity_match_does_not_collapse_actions_sharing_one_incidental_value(
    ledger: ActionLedger,
) -> None:
    """Admitting plain words must not make a shared adjective an identity.

    Both tickets are ``urgent``; only the title says which one. Matching on mere
    intersection would report the second as already done and silently never
    create it -- so recognition requires one token set to contain the other.
    """
    first = ledger.claim("ticket.create", {"priority": "urgent", "title": "alpha"})
    ledger.complete(first.key, external_id="T-1")

    second = ledger.claim("ticket.create", {"priority": "urgent", "title": "beta"})
    assert second.fresh, "a different ticket must not inherit the first one's identity"
    assert second.external_id is None


def test_identity_match_does_not_fire_for_an_explicit_key(ledger: ActionLedger) -> None:
    """An explicit key asserts identity; the token fallback must not overrule it."""
    first = ledger.claim("send_reminder", {"to": "x@y.z"}, key="reminder-monday")
    ledger.complete(first.key, external_id="msg_1")

    second = ledger.claim("send_reminder", {"to": "x@y.z"}, key="reminder-tuesday")
    assert second.fresh, "a distinct explicit key must still be a second action"


def test_identity_match_ignores_the_run_id_plumbing_token(ledger: ActionLedger) -> None:
    """continuum_run_id rides inside arguments and is common to every claim."""
    first = ledger.claim(
        "external.api_call",
        {"endpoint": "/a", "continuum_run_id": ledger.run_id},
    )
    ledger.complete(first.key, external_id="ok")

    other = ledger.claim(
        "external.api_call",
        {"endpoint": "/b", "continuum_run_id": ledger.run_id},
    )
    assert other.fresh, "a different endpoint is different work"


def test_a_larger_charge_is_not_deduplicated_against_a_smaller_one(
    ledger: ActionLedger,
) -> None:
    """Same payee, different amount, is a second charge -- not a retry of the first.

    The sharpest form of the containment rule. One token (the payee) is shared
    while the token that distinguishes the two actions is numeric, so this fails
    unless numbers count toward identity *and* a partial overlap is refused. The
    money is the whole distinction; suppressing the second charge reports success
    while the payment never happens.
    """
    first = ledger.claim("charge_card", {"recipient": "acct-4471", "amount_usd": 100})
    ledger.complete(first.key, external_id="txn-aaa", result={"charged": 100})

    second = ledger.claim("charge_card", {"recipient": "acct-4471", "amount_usd": 5000})

    assert second.fresh, "the $5000 charge was suppressed by the $100 one"
    assert second.action.arguments["amount_usd"] == 5000
    assert second.external_id is None


def test_a_differing_amount_counts_whether_it_is_a_number_or_a_string(
    ledger: ActionLedger,
) -> None:
    """Identity must not depend on the JSON type the caller happened to send.

    Over MCP an amount arrives as an ``int``; a client that quotes it sends a
    ``str``. Both draw the same distinction, so both must be honoured -- an agent
    cannot be expected to know that quoting a number changes whether its side
    effect runs.
    """
    first = ledger.claim("charge_card", {"recipient": "acct-4471", "amount_usd": "100"})
    ledger.complete(first.key, external_id="txn-aaa", result={"charged": "100"})

    second = ledger.claim("charge_card", {"recipient": "acct-4471", "amount_usd": "5000"})

    assert second.fresh

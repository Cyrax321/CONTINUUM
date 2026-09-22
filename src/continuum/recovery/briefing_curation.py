"""Curate resume context by provenance, not recency (issue #742).

The briefing used to render the newest agent-authored summary verbatim.
``docs/research/long_horizon_gaps.md`` names the failure mode: self-
conditioning. An error-laden summary from an abandoned attempt is the
*most recent* context by construction, and rehydrating it verbatim biases
every future session toward repeating the failed path.

CONTINUUM already holds the trustworthy half of the same information: the
sealed recovery contract (what is safe, what is invalidated), the validated
semantic state (verified evidence and findings), system-derived attempt
lessons (#313, from decisions and ledgers, never from an LLM), and bounded
trajectory reports (#393). This module decides what a new session should
rehydrate, deterministically and conservatively:

* **Provenance is explicit.** Every section is labeled ``verified``,
  ``system`` or ``agent`` - the briefing says where each fact came from,
  and agent material is always visually marked as self-authored.
* **Cautious ordering.** Verified and system-derived sections come before
  any agent-authored material, so the strongest evidence frames the
  session.
* **Quarantine, not deletion.** Stale or contradicted evidence and
  invalidated findings are listed with a reason under a quarantine header
  instead of disappearing - the resumed agent must know what NOT to trust.
  Agent summaries whose pinned environment no longer holds are dropped
  from the rehydrated context but remain in the log, recoverable through
  the ``--raw-summary`` diagnostic path in the CLI.
* **Bounded.** Section sizes are capped; identical inputs render
  byte-identically; no LLM, no clock, no randomness. The recovery verdict
  is an input, never an output: nothing here changes a mode or a safety
  result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from continuum.events import EventType
from continuum.models import StateStatus

if TYPE_CHECKING:
    from continuum.recovery.engine import RecoveryDecision
    from continuum.storage.base import Storage

__all__ = ["curate_briefing", "render_curated_briefing"]

#: Caps per section. The briefing is a hook payload - it rides into every
#: new session's context, so it must stay small by construction. Same
#: spirit as the 4096-char reasoning-summary cap.
_MAX_QUARANTINE_ITEMS = 8
_MAX_AGENT_PLAN_ITEMS = 3
_MAX_AGENT_DECISIONS = 3
_MAX_AGENT_OPEN_QUESTIONS = 3
_MAX_AGENT_WORKING_SET = 5

#: The three provenance tiers a section can carry, most trusted first.
_PROVENANCE_VERIFIED = "verified"
_PROVENANCE_SYSTEM = "system"
_PROVENANCE_AGENT = "agent"


def _latest_agent_summary(
    storage: Storage, run_id: str, decision: RecoveryDecision
) -> tuple[dict[str, Any], bool] | None:
    """The newest REASONING_SUMMARY payload and whether its pins still hold.

    Summaries are ``Origin.EXTERNAL_AGENT`` records (#235): the agent vouches
    for their own reasoning, CONTINUUM does not. A summary recorded with
    environment ``pinning`` (issue #241) is only as good as the pins. The
    check is the decision's own validation: a pinned resource that the
    fold now reports ``conflicted`` means the summary describes a world
    that no longer exists and is not rehydrated (it stays in the log).
    Unpinned summaries carry no world to contradict, so they pass.

    Returns ``(summary, pins_hold)`` or ``None`` when the run has no summary.
    """
    summaries = [e for e in storage.read_events(run_id) if e.type is EventType.REASONING_SUMMARY]
    if not summaries:
        return None
    payload = dict(summaries[-1].payload)
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    pinning = payload.get("pinning")
    if not isinstance(pinning, dict) or not pinning:
        return summary, True
    conflicted = {
        entry.component_id
        for entry in decision.validation.report.statuses
        if getattr(entry.status, "value", "") == "conflicted" and entry.component_id
    }
    # Pinning maps resource -> pinned version/hash; the resource name is the
    # identifier the validation report uses. Any overlap means the pinned
    # world has moved under the summary.
    pins_hold = not (set(pinning) & conflicted)
    return summary, pins_hold


def _quarantine(state: Any) -> list[dict[str, str]]:
    """Stale or contradicted items the resumed agent must not trust.

    Statuses other than VALID (stale, invalidated, requires_review - the
    ``StateStatus`` vocabulary) mean the fold has already judged the item;
    the briefing surfaces that judgment rather than silently omitting the
    item, because "I remember X" from a stale source is exactly the self-
    conditioning #742 guards against.
    """
    quarantined: list[dict[str, str]] = []

    def consider(kind: str, item: Any, label: str) -> None:
        status = getattr(item, "status", None)
        if status is None or status is StateStatus.VALID:
            return
        quarantined.append(
            {
                "kind": kind,
                "id": label,
                "reason": f"status {getattr(status, 'value', str(status))} - do not rely on this",
            }
        )

    for evidence in state.evidence:
        consider("evidence", evidence, evidence.summary or evidence.evidence_id)
    for finding in state.findings:
        consider("finding", finding, finding.claim)
    return quarantined[:_MAX_QUARANTINE_ITEMS]


def curate_briefing(storage: Storage, run_id: str, decision: RecoveryDecision) -> dict[str, Any]:
    """Build the curated resume context for ``run_id``. Pure and deterministic.

    Inputs are the sealed decision (contract, validated state, mode) and the
    run's own event log; nothing is written, no clock is read. The output
    carries three things the raw render never did:

    * a ``provenance`` tier on every section (``verified`` / ``system`` /
      ``agent``), most trusted first;
    * a ``quarantine`` list of stale or contradicted items with reasons;
    * a per-section ``reason`` string saying why the section is included.

    The agent summary is rehydrated only when its environment pins still
    hold; otherwise it is omitted from ``sections`` and noted in
    ``omitted`` with the reason, still recoverable from the log.
    """
    state = decision.state
    contract = decision.contract
    sections: list[dict[str, Any]] = []

    # -- verified: the sealed contract's own account ------------------------ #
    verified_lines = [f"recovery verdict: {decision.mode.value} (safe={decision.safe})"]
    if contract.reason:
        verified_lines.append(f"why: {contract.reason}")
    if contract.next_allowed_action:
        verified_lines.append(f"next permitted action: {contract.next_allowed_action}")
    sections.append(
        {
            "title": "recovery verdict (verified, sealed contract)",
            "provenance": _PROVENANCE_VERIFIED,
            "reason": "sealed by the recovery engine from the hash-chained log",
            "lines": verified_lines,
        }
    )

    # -- verified: validated state ------------------------------------------ #
    state_lines = [
        f"goal: {state.goal.description}",
        f"progress: {state.progress.completed}/{state.progress.total or '?'} completed"
        + (f", {state.progress.failed} failed" if state.progress.failed else ""),
        f"verified evidence: {sum(1 for e in state.evidence if e.status is StateStatus.VALID)} item(s)",
        f"verified findings: {sum(1 for f in state.findings if f.status is StateStatus.VALID)} item(s)",
    ]
    sections.append(
        {
            "title": "semantic state (verified, projected from events)",
            "provenance": _PROVENANCE_VERIFIED,
            "reason": "projected and validated against the current environment",
            "lines": state_lines,
        }
    )

    # -- system: attempt lessons and trajectory reports ---------------------- #
    if state.attempt_lessons:
        from continuum.recovery.summary import render_attempt_lesson

        lesson_lines: list[str] = []
        for lesson in state.attempt_lessons:
            lesson_lines += render_attempt_lesson(lesson)
        sections.append(
            {
                "title": "attempt lessons (system-derived)",
                "provenance": _PROVENANCE_SYSTEM,
                "reason": "derived from decisions and ledgers, never from an LLM (issue #313)",
                "lines": lesson_lines,
            }
        )
    if getattr(state, "trajectory_reports", None):
        from continuum.analysis.trajectory_report import render_trajectory_report
        from continuum.recovery.derived import is_derived_unverified

        trajectory_lines: list[str] = []
        for report in state.trajectory_reports:
            trajectory_lines += render_trajectory_report(report)
        # The distillation is always mechanical, but its *sources* may be
        # self-reported, and one such source makes the content unverified
        # no matter who projected it (#392). The title, not the reason, is
        # what renders, so the caveat has to live there to be seen.
        # is_derived_unverified reads only the derived_origin key.
        any_unverified = any(
            is_derived_unverified({"derived_origin": report.derived_origin})
            for report in state.trajectory_reports
        )
        sections.append(
            {
                "title": (
                    "trajectory reports (sleep-time, derived from unverified sources)"
                    if any_unverified
                    else "trajectory reports (sleep-time, system-derived)"
                ),
                # The tier follows the same rule as the title: self-reported
                # sources make the section agent-authority, not
                # system-authority, in the machine-readable payload (#392).
                "provenance": _PROVENANCE_AGENT if any_unverified else _PROVENANCE_SYSTEM,
                "reason": "distilled from archived history between sessions (issue #393)",
                "lines": trajectory_lines,
            }
        )

    # -- system: informed retry, engine-recorded ---------------------------- #
    if decision.informed_retry:
        from continuum.recovery.summary import render_informed_retry

        retry_lines = render_informed_retry(decision.informed_retry)
        if retry_lines:
            sections.append(
                {
                    "title": "what previous attempts changed (engine-recorded)",
                    "provenance": _PROVENANCE_SYSTEM,
                    "reason": "derived from the engine's own ledger of attempts (issue #265)",
                    "lines": retry_lines,
                }
            )

    # -- agent: the self-authored summary, only when its world still exists -- #
    omitted: list[dict[str, str]] = []
    latest = _latest_agent_summary(storage, run_id, decision)
    if latest is not None:
        summary, pins_hold = latest
        if pins_hold:
            sections.append(
                {
                    "title": "where the last session left off (self-authored, unverified)",
                    "provenance": _PROVENANCE_AGENT,
                    "reason": "newest agent summary; the agent vouches for it, CONTINUUM does not",
                    "lines": _render_agent_summary(summary),
                }
            )
        else:
            omitted.append(
                {
                    "kind": "agent summary",
                    "reason": "environment pins on the newest summary no longer hold; "
                    "read it via `continuum briefing <run_id> --raw-summary`",
                }
            )

    quarantine = _quarantine(state)
    return {
        "run_id": run_id,
        "sections": sections,
        "quarantine": quarantine,
        "omitted": omitted,
    }


def _render_agent_summary(summary: dict[str, Any]) -> list[str]:
    """The bounded agent-summary lines, same shape the old briefing rendered."""
    lines: list[str] = []
    for item in summary.get("plan_stack", [])[:_MAX_AGENT_PLAN_ITEMS]:
        lines.append(f"plan: {item}")
    for d in summary.get("decisions", [])[-_MAX_AGENT_DECISIONS:]:
        what = d.get("what", "")
        why = d.get("why", "")
        lines.append(f"decision: {what}" + (f" ({why})" if why else ""))
    for q in summary.get("open_questions", [])[:_MAX_AGENT_OPEN_QUESTIONS]:
        lines.append(f"open: {q}")
    working_set = summary.get("working_set", [])
    if working_set:
        lines.append(f"working set: {', '.join(map(str, working_set[:_MAX_AGENT_WORKING_SET]))}")
    return lines


def render_curated_briefing(curated: dict[str, Any]) -> list[str]:
    """Render the curated briefing as deterministic text lines.

    Sections appear in curation order - verified first, agent material last -
    each under its title with its provenance tier, followed by the quarantine
    block. Byte-identical for identical input.
    """
    lines: list[str] = []
    for section in curated["sections"]:
        lines.append(f"{section['title']}")
        for line in section["lines"]:
            lines.append(f"  {line}")
        lines.append("")
    if curated["quarantine"]:
        lines.append("do not trust (quarantined):")
        for item in curated["quarantine"]:
            lines.append(f"  [{item['kind']}] {item['id']} - {item['reason']}")
        lines.append("")
    for item in curated["omitted"]:
        lines.append(f"omitted: {item['kind']} - {item['reason']}")
    return lines

# CONTINUUM Security Extension — Results

This file records numbers for the Security Extension. It is filled in as the
work unit progresses; anything marked PENDING is not yet measured.

## Extension 1 — Secure Planning Loop

- Toy task (cookie-consent banner): PASSED. A high-risk branch gated behind a
  spoofed "Accept" label is caught (`contested` observation -> `REQUIRES_REVIEW`)
  and the action does not execute. With the gate disabled, the same page lets
  the attack succeed silently (before/after pair in
  `tests/test_toy_task_banner_attack.py`). Ledger trace: a `BRANCH_RESOLVED`
  event with `requires_review: true` is recorded.
- Mini-benchmark (5-10 tasks): PENDING. Per the spec sequencing, this runs
  only after the core mechanism is proven and the periodic revalidation
  extension is solid.

## Extension 2 — Periodic Revalidation

- Scheduling: PASSED. Revalidation fires on the step interval (default 25) and
  on app switch, verified by `tests/test_revalidation_schedule.py` against a
  run whose environment drifts mid-run. Drift is caught within one revalidation
  cycle, not only at the next crash.
- No regression: the existing crash/resume revalidation path is reused
  unchanged; the full suite remains green.
- Mini-benchmark (state coherence across an app switch): PENDING.

## What this does NOT solve

- Extension 1 does **not** defeat the underlying attack. A well-optimized
  adversarial pixel patch is still open per the CaMeLs paper. We add an audit
  trail and a risk-tiered human-escalation path so a manipulated branch is
  flagged and logged instead of firing silently.
- Extension 2 does **not** improve long-horizon reasoning. It catches drift
  earlier (on a schedule) by re-checking ground truth, reusing the existing
  crash/resume revalidation logic.
- Neither extension claims to have "solved" its source paper.

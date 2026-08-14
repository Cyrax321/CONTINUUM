# CONTINUUM Security Extension — Problem Statement

This document is the first deliverable of the Security Extension work unit
(spec Section 5, step 1). It states, per extension, the source paper, the
exact unsolved claim as of its publication date, and what we actually add.

Honest framing, repeated so it cannot be lost: each extension makes an
existing, unsolved gap **auditable and detectable**, and gates it behind
human review. Neither extension defeats the underlying attack. Nowhere do we
claim to have "solved" a paper's open problem.

---

## Extension 1 — Secure Planning Loop

- **Paper:** Foerster et al., *CaMeLs Can Use Computers Too*, arXiv:2601.09923
  (March 2026).
- **Problem named as unsolved:** *Branch steering.* An attacker manipulates what
  the perception model reports about the environment (a fake button label, an
  injected cookie banner, an adversarial pixel patch) and thereby steers the
  agent down a valid-but-malicious branch of its own approved plan. The plan is
  sound; the perception input that selected the branch is not.
- **What the paper itself says is still open:** a well-optimized adversarial
  pixel patch is not defeated by what we add. We do not claim otherwise.
- **Our fix (additive):** we extend CONTINUUM's existing, proven
  `REQUIRES_REVIEW → request_human` path with two new primitives:
  - `ObservationProvenance` — every perception claim is recorded with a
    trust tier (`verified` / `unverified` / `contested`), the verifier that
    produced it, a content hash of the screenshot/DOM slice, and the raw claim
    text (never mutated, for audit).
  - `PlanBranch` — the planner emits branches tagged with a `risk_tier`
    (`low` / `medium` / `high`) and whether the branch depends on a perception
    claim.
  - A trust gate (`resolve_branch`) combines the two: a high-risk branch
    resolved by anything other than a `verified` observation, or any
    `environment_observed` claim that is `contested`, is routed to
    `REQUIRES_REVIEW` and does not execute. A low-risk or `verified`
    observation branch proceeds.
- **What this does not solve:** it does not stop the manipulation of
  perception. It makes a manipulated branch get flagged and logged instead of
  firing silently, and gives a human an auditable trail to intercept.

---

## Extension 2 — Periodic Revalidation

- **Paper:** Yuan et al., *OSWorld 2.0*, arXiv:2606.29537 (June 2026).
- **Problem named as unsolved:** *Long-horizon state drift.* Agent effort
  scales with task length but success rate does not; coherence about "what
  state is the world actually in" silently degrades over hundreds of steps.
- **Our fix (additive):** CONTINUUM already revalidates semantic state against
  the environment at crash/resume (proven against real SIGKILL sessions). We
  add a scheduler that invokes the *same* revalidation logic during a normal,
  uninterrupted run: on a step interval (`step_interval`, default 25) and on
  app switch. No new comparison logic is written; only a new scheduling path
  into the existing, verified function.
- **What this does not solve:** it does not improve long-horizon reasoning. It
  catches drift earlier (within one revalidation cycle rather than only at the
  next crash) by re-checking ground truth on a schedule.

---

## Scope guardrails

Out of scope for this work unit: orchestration runtime, fine-tuning pipeline,
a full Hermes-style harness, and the other three sub-projects from the earlier
split. Only the secure planning loop and periodic revalidation are built.

The deliverable for each extension is a passing automated test plus a recorded
ledger trace, not a claim of victory. See `RESULTS.md` for the benchmark
numbers and the explicit "what this does not solve" section.

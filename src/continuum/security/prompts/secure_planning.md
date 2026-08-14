# Secure Planning Prompt Contract

This is the contract added to the planning system prompt for Extension 1
(Secure Planning Loop). It is intentionally declarative: the planner reasons
about intent and risk in the abstract and never sees live perception output
when assigning risk. This preserves the Dual-LLM isolation, the harness
combines the branch with observation provenance via the trust gate.

## Planner system prompt addition

When generating a plan, decompose it into branches. For each branch, output:
- branch_id
- action_intent: the concrete effect this branch has if executed
- risk_tier:
    "high"   — irreversible, moves money, deletes data, sends communication,
               changes account state
    "medium" — reversible but consequential (navigating to a page that
               changes context)
    "low"    — purely observational (scroll, read, screenshot)
- depends_on_observation: true/false — does taking this branch depend on a
  claim the perception model makes about the environment (button label,
  displayed form state, page content)?

Do not resolve a branch yourself. Emit it for the harness to resolve against
observation provenance.

## Notes for implementers

- The planner must not be given the raw screenshot or DOM slice when assigning
  `risk_tier`; risk is a property of the *action intent*, not of what
  perception reported.
- `depends_on_observation: true` is the signal the harness uses to require a
  `verify_observation` result before `resolve_branch` is called.
- `risk_tier: high` + anything other than a `verified` observation, or any
  `contested` `environment_observed` claim, routes to `REQUIRES_REVIEW` via
  `continuum.security.trust_gate.resolve_branch`. See docs/PROBLEM.md.

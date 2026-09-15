# Enforcement seams: making the verdict physical

The recovery engine's verdict is [advisory by
default](../recovery_walkthrough.md): `RecoveryEngine.assess()` answers
*where a run stands*, and nothing in the library stops a caller that ignores
the answer. A plain `pip install continuum-agent` gets the diagnosis, not a
guard.

This guide is the single place that answers the follow-up question: *if I
want the verdict enforced, where do I wire it?* There are four opt-in seams.
Each one refuses work the engine would not permit, at a different boundary.
Pick by what your agent can bypass.

| Seam | What it intercepts | How to enable | Fails when |
|:--|:--|:--|:--|
| Gate host hook | Structured tool calls, before they fire | `continuum hooks install claude-code --with-gate` (also `gemini`, `codex`), or `.continuum/gate.json` for the framework hooks | The call has no live claim, the effect already completed, or the outcome is uncertain |
| Enforcing HTTP gateway | Outbound HTTP from any language | `continuum gateway --port 8765` with `.continuum/gateway.json` | The request matches a registered route but has no live claim; the proxy settles the claim itself from the real status code |
| Replay guard | Framework node re-execution on resume | `continuum.replayguard`: `protected_call`, `langgraph_protected_node` | A replayed node tries to re-fire a side effect whose recorded outcome already exists (ACRFence replay hazard) |
| Observation hooks | The gap where the agent does not report | `continuum hooks install claude-code` (PostToolUse observe) | Not a refuser: it closes the blind window between real work on disk and the event log, so the *next* verdict is based on what actually happened |

## Which seam for which caller

- **Your agent runs inside a coding CLI or uses registered Python tools** -
  the gate hook. It is the cheapest seam and the only one that sees
  structured tool calls before they fire.
- **Your agent is not Python or makes raw HTTP calls** (Bash `curl`, another
  language, anything that skips the framework hooks) - the gateway. It
  interposes on the network boundary, which no agent code can route around
  once the proxy is the only egress.
- **Your framework replays nodes on resume** (LangGraph and friends) - the
  replay guard. It wraps the node so a re-execution returns the recorded
  result instead of firing the side effect twice.
- **Your agent does work outside the recording loop** - the observation
  hooks. They do not refuse anything; they make the verdict honest by
  feeding the log what the harness can see directly.

## What is enforced without any seam

One thing: the CLI's exit code. `continuum resume` maps the verdict to a
process status (only a verified-safe run exits `0`; `REQUEST_HUMAN` is `20`,
`ABORT`/`ROLLBACK` is `30`; see `src/continuum/cli/exitcodes.py`), so a
shell pipeline like `continuum resume "$RUN" && ./start-agent.sh` cannot
launch onto state the engine declared unsafe. This is enforcement at the
orchestration boundary, and it is on by design - but it only covers callers
that go through the CLI. A worker that calls `CheckpointManager.restore()`
directly bypasses it entirely; that caller needs one of the seams above, or
needs to honour `RecoveryDecision.permits()` itself.

## A note on layering

The seams share one substrate - the action ledger's claim semantics (a call
is allowed only when a live `STARTED` claim exists for its derived key) -
and they do not weaken each other. Wiring several is normal: a coding CLI
typically runs the observe hook plus the gate, and points anything that
escapes both at the gateway.

Related walkthroughs: [recovery_walkthrough.md](../recovery_walkthrough.md)
(the verdict and its contract), [liveness-watch.md](liveness-watch.md)
(`continuum watch`), [risk-policy.md](risk-policy.md) (risk triggers that
change the verdict itself).

# Enforcement: making the recovery verdict binding

`RecoveryEngine.assess` returns a `RecoveryDecision` that says what may safely
happen next. That verdict is **advisory**: it describes the answer, it does not
impose it. CONTINUUM is a library, not a supervisor. Nothing in `src/` calls
`RecoveryDecision.permits`, so a caller that restores a checkpoint directly can
resume a run the engine has already declared unsafe. This was observed in a real
crash test: the verdict was `REQUEST_HUMAN`, the worker called
`CheckpointManager.restore` rather than the CLI, and it continued to completion.

The engine is not broken. The contract is: CONTINUUM tells you the truth and
refuses to hand you a duplicate claim. It does not stop your process. Whether
that becomes a guarantee depends on which boundary your code crosses.

## One boundary that already enforces: the CLI

`continuum resume` honours the verdict by default through its exit codes. Only
a fully verified, safe-to-resume run exits 0; every other mode is non-zero, and
the mode-to-code map in `src/continuum/cli/exitcodes.py` is:

| Mode | Exit code | Meaning |
|:--|:--|:--|
| `RESUME` | 0 | Verified safe |
| `REPAIR_AND_RESUME`, `REPLAN` | 10 | Repair first |
| `WAIT`, `REQUEST_HUMAN` | 20 | A person must decide |
| `ROLLBACK`, `ABORT` | 30 | Not safe at all |

So a shell pipeline is safe by default:

```sh
continuum resume "$RUN" && ./start-agent.sh
```

The `&&` short-circuits on anything but `RESUME`, and the agent never launches
onto stale state. If you can route recovery through the CLI, you are done; read
no further.

## The four seams, for in-process work

The gap is code that resumes inside your own process, where no exit code is
involved. Four opt-in seams close it, each at a different boundary. None is on
by default, and a plain `pip install continuum-agent` gets none of them.

| Seam | Boundary | What it refuses | How you turn it on |
|:--|:--|:--|:--|
| [`gate`](../../src/continuum/gate.py) | Tool calls, at the host layer | Any side-effecting call without a live `STARTED` claim for its derived key | Register side-effect tools and key templates in `.continuum/gate.json`, and wire `gate.decide` into a harness `PreToolUse` hook that can deny |
| [`gateway`](../../src/continuum/gateway.py) | Outbound HTTP | Requests with no live claim, and duplicates of a claim already settled | `continuum gateway`, then point your app at `localhost:8765` instead of the upstream; routes in `.continuum/gateway.json` |
| [`replayguard`](../../src/continuum/replayguard.py) | Framework node re-execution | A second firing of a side effect when a framework replays node code on resume | Wrap the call in `protected_call`, or decorate the node with `langgraph_protected_node` |
| [`clienthooks`](../../src/continuum/clienthooks.py) | The agent's own tool calls | Nothing directly; it closes the observation window so a later verdict is not built on an incomplete log | `continuum hooks install`, which wires Claude Code `PostToolUse` hooks to `continuum observe` |

Choosing between them is a question about where the side effect lives:

- **In a tool call the model makes** - `gate`. This is the two-phase protocol
  made physical: `decide` answers one question, may this call proceed, and it
  denies unclaimed calls with instructions rather than silently letting them
  through. It fails closed when a config file exists but is malformed, because
  a file someone wrote is a statement of intent.
- **In outbound HTTP your code makes** - `gateway`. This is the only seam that
  sees calls no harness hook can observe, because the code under protection is
  not a tool call at all. It settles the claim itself after forwarding: 2xx/3xx
  complete, 4xx fails certain, 5xx and timeouts fail uncertain, since the effect
  may or may not have landed.
- **In framework node code that re-executes on resume** - `replayguard`. The
  ACRFence hazard (LangGraph issue #6208) is that interrupt-resume replays a
  node and its side effect fires twice; the guard memoises the completed
  operation and turns the replay into a cache hit instead.
- **Nowhere yet, but your agent writes files** - `clienthooks`. Strictly this is
  not enforcement but evidence: it records tool completions the log would
  otherwise miss, so the verdict you do enforce is built on complete
  information. Pair it with one of the other three.

`clienthooks` deserves one caution: observations are stamped
`Origin.EXTERNAL_AGENT`, deliberately. The fact they capture is asserted by the
client harness, not verified by CONTINUUM, so they can never launder
self-reported state into trusted state. They buy independent evidence a resumed
session can weigh against the log, not a certificate.

## Enforcing it yourself

If none of the seams fits, the hook is `RecoveryDecision.permits`. For a
process boundary, reuse the CLI's own mapping so your exit codes agree with
`continuum resume`:

```python
import sys

from continuum.cli.exitcodes import exit_code_for
from continuum.recovery.engine import RecoveryEngine

decision = RecoveryEngine(storage).assess(run_id)
if not decision.permits(next_action):
    sys.exit(exit_code_for(decision.mode))
```

For an in-process boundary, raise whatever your code already uses; the guard is
the check, not the exception type.

`permits` returns `True` for any action when the mode is `RESUME`, and otherwise
admits only `contract.next_allowed_action`, the single repair step the sealed
contract allows. Prefer it to testing `decision.safe` when you care about
per-action granularity: `safe` is `mode is RecoveryMode.RESUME`, so it blocks
everything including the one repair the engine wants you to perform.

## Why it is opt-in

Forcing enforcement inside a library call would surprise callers who
legitimately want to inspect a verdict and then override it, and a recovery
library that surprises people during a crash is worse than one that is
advisory. The exit-code contract is the one place enforcement is safe to
default on, because a shell pipeline has already opted into "tell me whether to
proceed". In-process, the choice is yours, and this page is where the choice is
documented.

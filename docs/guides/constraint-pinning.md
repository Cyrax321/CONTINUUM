# Constraint Pinning

> **Issues #420 and #391. Mechanism shipped in #416 to #419. This page documents the contract.**

Standing constraints issued once at session start ("never send without confirmation", "do not delete until asked") vanish exactly when they matter most: during context reconstruction. Briefing serves the newest summary, compaction archives the prefix, and nothing records whether a constraint survived. Constraint pinning makes those constraints first-class, verifiable events.

This guide shows how to record pins from harness hooks, how to verify them after reconstruction, and what the mechanism does and does not catch. It pairs with the harness hook recipes in `docs/recipes/` from #396 (cross-link only, not duplicated here). For lifecycle, statuses, grace windows, and strict-mode escalation, see the concepts page at `docs/concepts/constraint-pinning.md` and the API docs for `src/continuum/models.py` and `src/continuum/state/semantic.py`.

## Hash-only storage

Pins store only a SHA-256 digest, never the plaintext. The operator keeps the original text, CONTINUUM never sees it.

```python
import hashlib

from continuum.events import EventType
from continuum.models import ConstraintPinned, ConstraintRetracted

constraint_text = "never send without confirmation"
sha = hashlib.sha256(constraint_text.encode("utf-8")).hexdigest()  # 64 lowercase hex

pin = ConstraintPinned(constraint_id="no-send-without-confirm", sha256=sha)
storage.append_event(run_id, EventType.CONSTRAINT_PINNED, pin.model_dump())
```

Validation in `src/continuum/models.py` is `Field(strict=True)` plus a `field_validator` that requires `_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")` to `fullmatch` (exactly 64 lowercase hex, no uppercasing, no prefix, no normalization). `constraint_id` is a label, 1 to 128 chars from ASCII letters, digits, and `.` `_` `:` `-` (`_CONSTRAINT_ID_PATTERN`), enforced the same way. See `src/continuum/models.py:ConstraintPinned` and `ConstraintRetracted`.

Retract a pin without storing text:

```python
storage.append_event(
    run_id,
    EventType.CONSTRAINT_RETRACTED,
    ConstraintRetracted(constraint_id="no-send-without-confirm").model_dump(),
)
```

Retracting an id that was never pinned does not crash. It is recorded in `SemanticState.unmatched_pin_retractions` so the mismatch is visible.

## Recording pins from harness hooks

Pins are recorded by the harness, not by the model. Record once at session start, before any summarization or compaction.

### Generic harness (Python)

```python
import hashlib

from continuum.events import EventType
from continuum.models import ConstraintPinned

def pin_at_session_start(storage, run_id: str, constraint_id: str, text: str) -> None:
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    storage.append_event(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id=constraint_id, sha256=sha).model_dump(),
    )

pin_at_session_start(storage, run_id, "no-delete", "never delete until asked")
pin_at_session_start(storage, run_id, "no-send", "never send without confirmation")
```

### Claude Code hooks

`continuum hooks install claude-code` wires `continuum observe` for file digests. For pins, add a `SessionStart` hook that runs Python (no `continuum pin` CLI exists):

```python
# .claude/hooks/pin_constraints.py invoked from .claude/settings.json SessionStart
import hashlib
from continuum.events import EventType
from continuum.models import ConstraintPinned
from continuum.storage import SQLiteStorage

storage = SQLiteStorage("continuum.db")
for cid, text in [
    ("no-send", "never send without confirmation"),
    ("no-delete", "never delete until asked"),
]:
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    storage.append_event(run_id, EventType.CONSTRAINT_PINNED, ConstraintPinned(constraint_id=cid, sha256=sha).model_dump())
```

Or in `hooks.json` call the script, not a nonexistent CLI subcommand:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "python .claude/hooks/pin_constraints.py" }
        ]
      }
    ]
  }
}
```

See `docs/recipes/claude-code.md` and `docs/guides/embed-claude-code.md` for the full SessionStart and PreCompact hook recipes from #396. This page does not duplicate those recipes, it only shows the pin write itself.

### GenericAgentAdapter

```python
from continuum.adapters.generic import GenericAgentAdapter
import hashlib
from continuum.events import EventType
from continuum.models import ConstraintPinned

adapter = GenericAgentAdapter(storage)
sha = hashlib.sha256(b"never delete until asked").hexdigest()
storage.append_event(
    run_id,
    EventType.CONSTRAINT_PINNED,
    ConstraintPinned(constraint_id="no-delete", sha256=sha).model_dump(),
)
```

## Statuses: present, absent, unverifiable

Every reconstruction path (briefing, resume banner, checkpoint rehydration) must account for every active pin as one of three statuses. The verdict comes from hash-tagged markers in the produced context, not from a summarizer self-report.

Marker format per pin: `[pin:<constraint_id>:<sha256[:8]>]` where `[:8]` is the first 8 hex chars. Emitted by `pin_markers_for_state(state)` and checked by `account_pins_in_context(state, context)`. See `src/continuum/state/semantic.py:account_pins_in_context` and `src/continuum/checkpoint/context.py:build_recovery_context`.

| Status | Meaning | When |
| --- | --- | --- |
| `present` | Marker found in context | Reconstructed context contains the pin, silent pass |
| `absent` | Marker not found, context not truncated | Summarizer dropped the constraint, next resume names `pin id:hash_prefix` past grace window raises a contract flag |
| `unverifiable` | Marker not found but context was truncated (`[context truncated` or `omitted:` present) | Cannot tell if pin was in dropped section, flagged but not as `absent` |

### Example: present

```python
from continuum.checkpoint.context import build_recovery_context
from continuum.state.semantic import account_pins_in_context, project

state = project(run_id, storage.read_events(run_id) + storage.read_archived_events(run_id))
context = build_recovery_context(state).render()  # includes ACTIVE CONSTRAINTS markers
accounting = account_pins_in_context(state, context)

# accounting["no-send"] == {"status": "present", "sha256": "abc...", "sha256_prefix": "abc12345", "flag": None, ...}
```

`resume --json` and `validate --json` render `present` silently under `constraint_pins.pins`, with `flagged: []`.

### Example: absent

```python
# Simulate a summary that dropped one constraint
marker = f"[pin:no-send:{sha[:8]}]"
context_without = context.replace(marker, "")
accounting = account_pins_in_context(state, context_without, grace_seconds=3600)

# accounting["no-send"]["status"] == "absent"
# if age past grace: flag == "pin no-send:abc12345 absent past grace (4000s > 3600s)"
```

`constraint_pins` surfaces `status: absent` and lists the pin in `flagged`. CLI text renders `  [!!] no-send:abc12345 absent -- pin no-send:abc12345 absent past grace (4000s > 3600s)`.

### Example: unverifiable

```python
truncated_context = context_without + "\n\n[context truncated to fit budget; omitted: ACTIVE CONSTRAINTS]"
accounting = account_pins_in_context(state, truncated_context)

# accounting["no-send"]["status"] == "unverifiable"
# flag == "pin no-send:abc12345 unverifiable past grace (4000s > 3600s)" only if past grace
```

`unverifiable` is flagged when past grace, but the flag text and `status` distinguish it from `absent`. `flagged` still contains the pin id so operators do not miss it.

## Knobs

### grace_seconds

`grace_seconds` is the window during which an `absent` pin is noted but not flagged as past grace. It is `int | None` per call, not global state. Default `None` means no deadline, so `past_grace` stays false and no absent flag is raised.

```python
from datetime import timedelta

pin = state.pins["no-send"]
now_within = pin.pinned_at + timedelta(seconds=10)
accounting = account_pins_in_context(state, context_without, grace_seconds=60, now=now_within)
# status == "absent", past_grace == False, flag is None

now_past = pin.pinned_at + timedelta(seconds=100)
accounting = account_pins_in_context(state, context_without, grace_seconds=60, now=now_past)
# status == "absent", past_grace == True, flag is "pin no-send:abc12345 absent past grace (100s > 60s)"
```

The JSON block surfaces `grace_deadline` per pin (`pinned_at + grace_seconds` as ISO-8601) and `past_grace` so callers do not recompute it. `grace_seconds: 0` flags any absent pin older than 0 seconds.

### strict

`strict` controls escalation, not detection. Detection always runs, `strict` decides whether a flagged absent pin escalates to `REQUIRES_REVIEW`.

```python
from continuum.state.semantic import check_pin_accounting

# Advisory only
accounting, flags, should_escalate = check_pin_accounting(state, context_without, grace_seconds=60, now=now_past, strict=False)
# flags == ["pin no-send:abc12345 absent past grace (100s > 60s)"]
# should_escalate == False

# Strict fail-closed
accounting, flags, should_escalate = check_pin_accounting(state, context_without, grace_seconds=60, now=now_past, strict=True)
# should_escalate == True -> caller that enforces fail-closed should set mode=REQUEST_HUMAN, safe=False (accounting layer, not automatic in RecoveryEngine v1)
```

Within grace, even `strict=True` does not escalate:

```python
accounting, flags, should_escalate = check_pin_accounting(state, context_without, grace_seconds=60, now=now_within, strict=True)
# flags == [], should_escalate == False
```

`check_pin_accounting` returns `(accounting, flags, should_escalate)` where `should_escalate` is true only when `strict` and at least one `absent` pin is `past_grace`. `unverifiable` never triggers `should_escalate`, even past grace.

## Verifying after reconstruction

After `compact` plus `briefing`, build the context and classify:

```python
from continuum.checkpoint.context import build_recovery_context
from continuum.state.semantic import account_pins_in_context, constraint_pins_payload

state = project(run_id, storage.read_events(run_id) + storage.read_archived_events(run_id))
ctx = build_recovery_context(state).render()
block = constraint_pins_payload(state, ctx, grace_seconds=3600)
# block == {"pins": {"no-send": {"status": "absent", ...}}, "flagged": ["no-send"], "grace_seconds": 3600}
```

`resume --json` and `validate --json` include the same block via `constraint_pins_payload(state, rendered_context)`:

```json
{
  "constraint_pins": {
    "pins": {
      "no-send-without-confirm": {
        "status": "absent",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sha256_prefix": "e3b0c442",
        "pinned_at": "2026-08-29T10:00:00Z",
        "grace_deadline": "2026-08-29T11:00:00Z",
        "past_grace": true,
        "flag": "pin no-send-without-confirm:e3b0c442 absent past grace (4000s > 3600s)"
      }
    },
    "flagged": ["no-send-without-confirm"],
    "grace_seconds": 3600
  }
}
```

CLI text renders flagged pins prominently; piped output is byte-stable modulo TTY colour per #419:

```text
CONSTRAINT PINS: flagged pins require attention
  [!!] no-send-without-confirm:e3b0c442 absent -- pin no-send-without-confirm:e3b0c442 absent past grace (4000s > 3600s)
```

Compaction: pins live in the live chain and survive anchoring like any event. `compact_run` moves the pre-anchor prefix to `events_archive`, the live chain keeps the anchor as trusted genesis, `verify` walks anchored logs, `project` merges archived plus live, and pins survive. `checkpoint restore` also restores pins. Replaying old runs produces identical results with no pins recorded (feature off by default for existing data).

## Threat model: what pinning catches and what it does not

**Catches: silent drops across compaction and briefing**

- Briefing serves the newest summary that omits a constraint, compaction archives the prefix, context truncation drops the section containing a pin marker, the next `resume` names `pin id:hash_prefix` and flags when past grace, and `strict` escalates to `REQUIRES_REVIEW` (house fail-closed).
- Coordinates with the detector-side tripwire in the SNAGLINE companion repo #90: CONTINUUM enforces re-injection and flags drops, that detector independently verifies from telemetry.

**Does not catch: adversarial contexts that forge presence markers are detectable but out of scope for v1**

- A summarizer that forges the marker `[pin:id:hash8]` without actually preserving the constraint text will appear `present`. The marker is evidence of presence in the produced text, not proof that the agent will honor the constraint. Detecting that requires an external detector that checks whether the agent actually honored the constraint, not just mentioned it.
- Plaintext not stored, so pin text cannot be recovered from `sha256` alone. The operator must keep the original text to recompute and verify.
- No network, transport, or privacy guarantee beyond hash-only storage. No rate limiting or authenticated identity for who pinned.

### Honest does-not-solve list

| Case | Caught | Note |
| --- | --- | --- |
| Summarizer omits `never delete` | Yes, `absent` past grace flagged | Strict escalates to `REQUIRES_REVIEW` |
| Context truncated before pin section | `unverifiable` flagged, not `absent` | Distinguishes dropped from absent |
| Pin retracted then re-pinned | Active with new `pinned_at` and `sha256` | Grace window restarts |
| Retracted unknown pin | Recorded in `unmatched_pin_retractions` without crash | Degrades gracefully |
| Forged marker without enforcement | No, out of scope v1 | Detector needed, see SNAGLINE #90 |
| Full disk rewrite of DB and ledger | No | Tamper evident, not tamper proof |
| Model fabricates evidence before it reaches the log | No | Outside recovery boundary |

## References

- `src/continuum/models.py:ConstraintPinned`, `ConstraintPin`, `SemanticState.pins` and `unmatched_pin_retractions`
- `src/continuum/state/semantic.py:account_pins_in_context`, `pin_markers_for_state`, `check_pin_accounting`, `constraint_pins_payload` (#418, #419)
- `src/continuum/checkpoint/context.py:build_recovery_context` marker emission and accounting integration
- `docs/concepts/constraint-pinning.md` for lifecycle and escalation details
- `docs/recipes/` harness hook recipes #396 (cross-link only)
- `docs/threat_model.md` for the full honest scope list

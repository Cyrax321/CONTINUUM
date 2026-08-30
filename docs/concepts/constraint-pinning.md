# Constraint Pinning

> **Issues #391 and #420. Mechanism shipped in #416 (events), #417 (projection), #418 (accounting), #419 (surface).**

Standing constraints ("never send without confirmation", "do not delete until asked") are issued once at session start and vanish precisely when reconstruction needs them. Briefing serves the newest summary, compaction archives the prefix, and a context window can truncate the section that held the constraint. Pinning makes those constraints first-class, verifiable events that survive every reconstruction path.

For how to record pins from harness hooks with hash-only storage, see `docs/guides/constraint-pinning.md`. For the full honest scope, see `docs/threat_model.md`.

## Lifecycle

### 1. Pin

```text
CONSTRAINT_PINNED {constraint_id, sha256}
```

- `constraint_id`: 1 to 128 chars from `[A-Za-z0-9._:-]`. Narrow on purpose, the hash keeps the text private and the id must not become a side channel for it.
- `sha256`: 64 lowercase hex chars, the SHA-256 of the exact constraint text as UTF-8 bytes, never the text itself. `models.py` validates `sha256` via `Field(strict=True)` plus a `field_validator` that requires `_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")` to `fullmatch`. Uppercase, short, prefixed, or non-hex digests are refused at the boundary, not normalized. See `src/continuum/models.py:ConstraintPinned`.

```python
import hashlib
from continuum.events import EventType
from continuum.models import ConstraintPinned

sha = hashlib.sha256("never send without confirmation".encode("utf-8")).hexdigest()
storage.append_event(run_id, EventType.CONSTRAINT_PINNED, ConstraintPinned(constraint_id="no-send", sha256=sha).model_dump())
```

### 2. Project

`project(run_id, events)` folds pins into `SemanticState`:

- `pins: dict[str, ConstraintPin]` keyed by `constraint_id`, each with `constraint_id`, `sha256`, `status="active"`, `provenance`, and `pinned_at`.
- `unmatched_pin_retractions: list[str]` for ids retracted without a matching active pin. This degrades gracefully rather than crashing, so a retraction that lived in an archived prefix or never existed is visible without guessing.

Re-pinning the same `constraint_id` replaces the entry with a new `sha256` and `pinned_at`, the grace window restarts from the new pin time.

### 3. Retract

```text
CONSTRAINT_RETRACTED {constraint_id}
```

Records the id alone (`Field(strict=True)`, same label charset). See `src/continuum/models.py:ConstraintRetracted`.

```python
from continuum.models import ConstraintRetracted
storage.append_event(run_id, EventType.CONSTRAINT_RETRACTED, ConstraintRetracted(constraint_id="no-send").model_dump())
```

If `constraint_id` is active, it is deleted from `pins`. Otherwise it is appended to `unmatched_pin_retractions`.

### 4. Survive compaction and checkpoints

Pins live in the live chain and survive anchoring like any event. `compact_run` moves the pre-anchor prefix to `events_archive`, the live chain keeps the anchor as trusted genesis, `verify` walks anchored logs, `project` merges archived plus live, and pins survive. `CheckpointManager` seals and restores the projected `pins` dict as part of `SemanticState`, so `checkpoint restore` also restores pins.

Replaying old runs that never pinned produces identical results with an empty `pins` dict (feature off by default for existing data).

### Diagram

```text
SessionStart  --CONSTRAINT_PINNED-->  Event log  --project-->  SemanticState.pins
      |                                   |                          |
      v                                   v                          v
 hash-only                         verify walks chain         build_recovery_context
 (no plaintext)                    and anchored archive       emits [pin:id:hash8] markers
                                      |                          |
                                      v                          v
                              compact preserves pins     account_pins_in_context classifies
                                                                 as present/absent/unverifiable
```

## Statuses: present, absent, unverifiable

Every reconstruction path, briefing, resume banner, checkpoint rehydration, must account for every active pin as one of three statuses. The verdict is computed from hash-tagged markers in the produced context, not from a summarizer self-report.

Marker format per pin: `[pin:<constraint_id>:<sha256[:8]>]` where `[:8]` is the first 8 hex chars. Emitted by `pin_markers_for_state(state)` and checked by `account_pins_in_context(state, context)`. See `src/continuum/state/semantic.py:account_pins_in_context` and `src/continuum/checkpoint/context.py:_pins_section`.

| Status | Meaning | When | Flag |
| --- | --- | --- | --- |
| `present` | Marker found in context | Reconstructed context contains the pin | Never flagged, silent pass |
| `absent` | Marker not found, context not truncated | Summarizer dropped the constraint | Flagged when past grace, escalates in strict mode |
| `unverifiable` | Marker not found but context was truncated (`[context truncated` or `omitted:` present) | Cannot tell if pin was in dropped section | Flagged when past grace, never escalates to `REQUIRES_REVIEW` |

All three appear in the JSON block `constraint_pins.pins[<id>].status` surfaced by `resume --json` and `validate --json` via `constraint_pins_payload` (#419), and in the per-pin dict returned by `account_pins_in_context` (see `src/continuum/state/semantic.py:constraint_pins_payload`).

### Example: present

```python
from continuum.checkpoint.context import build_recovery_context
from continuum.state.semantic import account_pins_in_context, project

state = project(run_id, storage.read_events(run_id))
context = build_recovery_context(state).render()
accounting = account_pins_in_context(state, context)
# accounting["no-send"]["status"] == "present"
# accounting["no-send"]["flag"] is None
# accounting["no-send"]["past_grace"] == False when grace_seconds is None
```

Rendered context contains:

```text
ACTIVE CONSTRAINTS
  no-send:abc12345 [pin:no-send:abc12345]
```

### Example: absent

```python
marker = f"[pin:no-send:{sha[:8]}]"
context_without = context.replace(marker, "")
accounting = account_pins_in_context(state, context_without, grace_seconds=3600)
# accounting["no-send"]["status"] == "absent"
# flag is None within grace, "pin no-send:abc12345 absent past grace (4000s > 3600s)" past grace
```

`absent` names the pin by `id:hash_prefix` instead of resuming silently.

### Example: unverifiable

```python
truncated = context_without + "\n\n[context truncated to fit budget; omitted: ACTIVE CONSTRAINTS]"
accounting = account_pins_in_context(state, truncated, grace_seconds=3600)
# accounting["no-send"]["status"] == "unverifiable"
# never "absent", even past grace, flag text says "unverifiable past grace"
```

The distinction matters. `absent` means the marker was in the budget and is missing, `unverifiable` means the marker would have been in a section that was dropped, so the absence is not evidence of a drop.

## Grace windows

`grace_seconds` is a per-call knob (`int | None`), not global state. Default `None` means no deadline, so `past_grace` stays false and no absent flag is raised.

```text
age_seconds = now - pinned_at
past_grace = grace_seconds is not None and age_seconds > grace_seconds
```

`pinned_at` is the timestamp of the `CONSTRAINT_PINNED` event. Each pin carries its own deadline `grace_deadline = pinned_at + grace_seconds` (ISO-8601 in the JSON block).

When `status` is `absent` and `past_grace` is true, the dict carries a flag string:

```text
pin no-send:abc12345 absent past grace (4000s > 3600s)
```

When `status` is `unverifiable` and `past_grace` is true, the flag says `unverifiable past grace`. When `status` is `present`, there is never a flag regardless of age.

### Example: grace window configurable

```python
from datetime import timedelta

pin = state.pins["no-send"]
now_within = pin.pinned_at + timedelta(seconds=10)
accounting = account_pins_in_context(state, context_without, grace_seconds=60, now=now_within)
# status == "absent", past_grace == False, flag is None

now_past = pin.pinned_at + timedelta(seconds=100)
accounting = account_pins_in_context(state, context_without, grace_seconds=60, now=now_past)
# status == "absent", past_grace == True, flag == "pin no-send:abc12345 absent past grace (100s > 60s)"

# No grace configured
accounting = account_pins_in_context(state, context_without)
# past_grace == False, flag is None even though absent

# Zero grace is immediate
accounting = account_pins_in_context(state, context_without, grace_seconds=0, now=now_within)
# past_grace == True, flag is not None
```

The JSON payload mirrors this:

```json
{
  "pins": {
    "no-send": {
      "status": "absent",
      "sha256": "abc123...",
      "sha256_prefix": "abc12345",
      "pinned_at": "2026-08-29T10:00:00Z",
      "grace_deadline": "2026-08-29T11:00:00Z",
      "past_grace": true,
      "flag": "pin no-send:abc12345 absent past grace (100s > 60s)"
    }
  },
  "flagged": ["no-send"],
  "grace_seconds": 3600
}
```

`flagged` is the sorted list of pin ids whose `status` is not `present`, regardless of grace.

## Strict-mode REQUIRES_REVIEW escalation

`strict` controls escalation, not detection. Detection always runs.

```python
from continuum.state.semantic import check_pin_accounting

# Advisory
accounting, flags, should_escalate = check_pin_accounting(state, context_without, grace_seconds=60, now=now_past, strict=False)
# flags == ["pin no-send:abc12345 absent past grace (100s > 60s)"]
# should_escalate == False

# Strict fail-closed
accounting, flags, should_escalate = check_pin_accounting(state, context_without, grace_seconds=60, now=now_past, strict=True)
# should_escalate == True

# Within grace, even strict does not escalate
accounting, flags, should_escalate = check_pin_accounting(state, context_without, grace_seconds=60, now=now_within, strict=True)
# flags == [], should_escalate == False
```

`should_escalate` is true only when `strict` and at least one pin is `absent` and `past_grace`. `unverifiable` past grace is flagged but never triggers `should_escalate`, even in strict mode (house fail-closed applies to the cases we can assert, not to those we cannot tell).

`check_pin_accounting(..., strict=True)` returns `should_escalate` true when at least one `absent` pin is `past_grace`. A caller that enforces fail-closed should treat that as `REQUIRES_REVIEW` (`REQUEST_HUMAN` with `safe=False`), matching the house rule that uncertainty degrades rather than resolves in its own favor. The current `build_recovery_context` surfaces flags in `notes` and the CLI/MCP surfaces the `constraint_pins` block read-only; strict escalation remains in the accounting layer and is consumed by callers rather than by an automatic mode change in `RecoveryEngine` v1 (see `src/continuum/state/semantic.py:check_pin_accounting` and `src/continuum/checkpoint/context.py:build_recovery_context`).

## Edge cases

| Case | Result |
| --- | --- |
| Pin retracted then re-pinned | Active with new `pinned_at` and `sha256`, grace window restarts |
| Retracted unknown pin | Recorded in `unmatched_pin_retractions`, no crash |
| No pins recorded | `pins` empty, `flagged` empty, `grace_seconds` as passed |
| Context truncated before pin section | `unverifiable`, not `absent` |
| Forged marker `[pin:id:hash8]` | Appears `present` (detectable but out of scope v1) |

## References

- `src/continuum/models.py:ConstraintPinned`, `ConstraintRetracted`, `ConstraintPin`, `SemanticState.pins`
- `src/continuum/state/semantic.py:account_pins_in_context`, `pin_markers_for_state`, `check_pin_accounting`, `constraint_pins_payload` (#418, #419)
- `src/continuum/checkpoint/context.py:build_recovery_context` marker emission and pin accounting integration
- `docs/guides/constraint-pinning.md` for the how-to (recording pins from harness hooks)
- `docs/threat_model.md` for honest does-not-solve list

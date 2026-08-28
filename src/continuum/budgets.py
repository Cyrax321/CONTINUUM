"""Run-level retry budgets (issue #240).

Agent loops invent retries: a failing upstream gets hammered because the
model re-plans after every failure, and each attempt opens a fresh ledger
slot. RetryGuard (arXiv:2511.23278) shows local retry policies amplify cost;
the fix here is a *run-level budget* evaluated at claim time.

Registries live in `.continuum/budgets.json` (JSON, matching the other
registries):

    {"default_max_attempts": 3,
     "action_types": {"send_invoice": {"max_attempts": 5}}}

`evaluate_budget` counts prior attempts for an action type from the folded
ledger and returns whether another claim may proceed. CONTINUUM never retries
anything itself - it counts and gates - so the enforcement surface stays a
single pure function plus thin wiring at claim sites.

Attempts are counted per idempotency key, not per action type (issue #368). The
limit is still configured per type, because that is the unit an operator thinks
in, but what it caps is repetition of one operation. Counting per type made
distinct work compete for the same allowance: three different recipients each
failing once, with no retry anywhere, exhausted a budget of three and blocked a
fourth that had never been attempted.

Authorization-bound budgets (issue #411) add an optional section keyed by
``(action_type, authorization_id)``, giving one logical authorization a durable
monotonic counter however many fresh idempotency keys get minted for it (epic
#390):

    {"authorization_bound": {"send_invoice":
         {"authz:stripe-cust-1": {"counter": 2, "max_attempts": 5}}}}

Configs written before the section existed load unchanged and read as unbound,
which is exactly today's behaviour. ``get_remaining``, ``increment`` and
``would_refuse`` read and maintain entries purely; ``save_budgets`` persists
them. Nothing gates on the section yet (that wiring lands with issue #413).

Where the registry asks for an integer it means one: JSON ``true`` is refused
rather than read as a silent cap of 1 (issue #429), and a rejection names the
offending value and its type (issue #326). ``save_budgets`` stages and renames
rather than truncating in place, so a process that dies mid-write costs at most
the last increment, never the registry (issue #427). Because that replaces an
inode instead of rewriting one, the staged file inherits the target's
permissions and ownership, a symlinked registry is written through rather than
replaced, and the rename is flushed: swapping the file must not change who can
read it, which file is written, or whether the write survives the crash it
guards against.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

__all__ = [
    "DEFAULT_BUDGETS_PATH",
    "AUTHORIZATION_BOUND_KEY",
    "attempts_by_key",
    "attempts_for_type",
    "BudgetConfigError",
    "load_budgets",
    "save_budgets",
    "evaluate_budget",
    "backoff_delay",
    "get_remaining",
    "increment",
    "would_refuse",
]

DEFAULT_BUDGETS_PATH = ".continuum/budgets.json"

#: Registry key of the optional section keyed by (action_type, authorization_id).
AUTHORIZATION_BOUND_KEY = "authorization_bound"

#: Fallback when neither the action type nor the registry sets a limit.
FALLBACK_MAX_ATTEMPTS = 3

#: Where Linux publishes the process umask (kernel 4.7+). Absent elsewhere, and
#: named rather than inlined so a test can take the fallback path on any platform.
_UMASK_STATUS_PATH = "/proc/self/status"

#: Whether ownership can be written back at all. There is no POSIX owner or group
#: on Windows for a replaced registry to lose in the first place, and ``os.chown``
#: does not exist there.
_CAN_CHOWN = sys.platform != "win32"


class BudgetConfigError(ValueError):
    """The budget registry exists but cannot be honoured."""


def _is_int(value: Any) -> TypeGuard[int]:
    """Whether ``value`` is an integer *and not* a JSON boolean (issue #429).

    ``isinstance(True, int)`` holds in Python, so every plain int check in this
    file used to pass for JSON ``true``: it silently meant a cap of 1 as a
    ``max_attempts``, and a ``counter`` of ``true`` became 2 after one
    increment. A registry whose contract elsewhere is to fail loudly instead
    quietly meant something other than what was written, so booleans are
    refused here rather than coerced.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _offending(value: Any) -> str:
    """Name the value and its type, so a rejection says what was wrong (issue #326).

    A registry hand-converted from YAML arrives with ``3.0`` where ``3`` was
    meant; the bare "needs a positive integer" the operator used to get is the
    same sentence for a missing field, a float, a string and a boolean, and it
    never points at the token to change.
    """
    return f", got {value!r} ({type(value).__name__})"


def load_budgets(path: Path) -> dict[str, Any]:
    """Read the budget registry. ``{}`` when absent; raise when malformed."""
    if not path.exists():
        return {}
    # Absolute, so the message names a file the operator can open: the
    # relative form depends on the cwd of whatever loaded the registry
    # (a hook, the sidecar, a CI step). Matches gate.py per #333.
    location = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BudgetConfigError(f"{location} is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise BudgetConfigError(f"{location}: expected a JSON object")
    action_types = raw.get("action_types", {})
    if not isinstance(action_types, dict):
        raise BudgetConfigError(f"{location}: 'action_types' must be an object")
    for name, spec in action_types.items():
        entry = spec.get("max_attempts") if isinstance(spec, dict) else spec
        if not _is_int(entry) or entry < 1:
            raise BudgetConfigError(
                f"{location}: action type {name!r} needs a positive integer "
                f"'max_attempts'{_offending(entry)}"
            )
    default_max = raw.get("default_max_attempts")
    if default_max is not None and (not _is_int(default_max) or default_max < 1):
        raise BudgetConfigError(
            f"{location}: 'default_max_attempts' must be >= 1{_offending(default_max)}"
        )
    _validate_authorization_bound(raw, location)
    return raw


def _max_for(action_type: str, raw: Mapping[str, Any]) -> int:
    per_type = raw.get("action_types", {})
    spec = per_type.get(action_type)
    if isinstance(spec, int):
        # int(), not the value itself: :func:`load_budgets` refuses booleans, but
        # this stays reachable with a hand-built mapping, and a bool leaking out
        # here renders as JSON ``true`` in the `continuum budget` report.
        return int(spec)
    if isinstance(spec, dict) and isinstance(spec.get("max_attempts"), int):
        return int(spec["max_attempts"])
    fallback = raw.get("default_max_attempts", FALLBACK_MAX_ATTEMPTS)
    return int(fallback)


def attempts_by_key(events: Any, action_type: str) -> dict[str, int]:
    """Unsettled claim attempts for ``action_type``, counted per idempotency key.

    A retry budget has to count retries of *one operation*. Counting per action
    type instead conflated distinct work with repetition: three different
    recipients each failing once, with no retry anywhere, exhausted a budget of
    three and blocked a fourth recipient that had never been attempted (issue
    #368). Any fan-out with more than ``max_attempts`` failures of one type
    deadlocked mid-run.

    The key is the right unit because it *is* the operation's identity, and it is
    stable across retries: re-claiming after FAILED or COMPENSATED copies the
    existing action, so successive attempts under one key accumulate here rather
    than each opening a fresh row.

    A claim slot (an ``ACTION_RECORDED`` whose action status is STARTED) is one
    attempt. Settlement events are updates, not new attempts, so retries count but
    their bookkeeping does not. Keys whose action went on to COMPLETE are omitted:
    an operation that succeeded was never retried (issue #309).
    """
    from continuum.events import EventType
    from continuum.models import ActionStatus

    slots: dict[str, int] = {}
    final: dict[str, str] = {}
    for event in events:
        if event.type is not EventType.ACTION_RECORDED:
            continue
        action = event.payload.get("action")
        if not isinstance(action, Mapping) or action.get("action_type") != action_type:
            continue
        key = str(event.payload.get("key", ""))
        if not key:
            continue
        status = str(action.get("status"))
        if status == ActionStatus.STARTED.value:
            slots[key] = slots.get(key, 0) + 1
        final[key] = status

    return {
        key: count for key, count in slots.items() if final.get(key) != ActionStatus.COMPLETED.value
    }


def attempts_for_type(events: Any, action_type: str) -> int:
    """The most attempts any single operation of ``action_type`` has used.

    Reports the figure the budget is actually compared against, so the ``continuum
    budget`` view agrees with what the claim site enforces. It is deliberately not
    the sum across keys: that total is a measure of how much distinct work a run
    did, which no limit here caps (issue #368).
    """
    per_key = attempts_by_key(events, action_type)
    return max(per_key.values(), default=0)


def evaluate_budget(
    raw_config: Mapping[str, Any] | None,
    action_type: str,
    attempts_so_far: int,
) -> tuple[bool, int, int]:
    """Return ``(allowed, attempts_so_far, max_attempts)``.

    Pure so claim sites can call it with nothing but the folded attempt count.
    """
    _ = raw_config  # kept in signature for symmetry with other registries
    cfg = raw_config or {}
    maximum = _max_for(action_type, cfg)
    return attempts_so_far < maximum, attempts_so_far, maximum


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
) -> float:
    """Exponential backoff with a ceiling. Pure; jitter is the caller's job."""
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return float(min(base * (2 ** (attempt - 1)), cap))


# --- authorization-bound budgets (issue #411) --------------------------------------- #


def _validate_authorization_bound(raw: Mapping[str, Any], location: Path) -> None:
    """Shape-check the optional authorization-bound section of a loaded registry.

    Absent means unbound, which is valid: configs without authorization data
    must keep loading exactly as they always did (epic #390).
    """
    section = raw.get(AUTHORIZATION_BOUND_KEY)
    if section is None:
        return
    if not isinstance(section, dict):
        raise BudgetConfigError(f"{location}: '{AUTHORIZATION_BOUND_KEY}' must be an object")
    for action_type, entries in section.items():
        if not isinstance(entries, dict):
            raise BudgetConfigError(
                f"{location}: authorization-bound entries for {action_type!r} must be an object"
            )
        for authorization_id, entry in entries.items():
            label = f"{location}: authorization-bound entry {action_type!r}/{authorization_id!r}"
            if not isinstance(entry, dict):
                raise BudgetConfigError(f"{label} must be an object")
            counter = entry.get("counter", 0)
            if not _is_int(counter) or counter < 0:
                raise BudgetConfigError(
                    f"{label} needs a non-negative integer 'counter'{_offending(counter)}"
                )
            max_attempts = entry.get("max_attempts")
            if not _is_int(max_attempts) or max_attempts < 1:
                raise BudgetConfigError(
                    f"{label} needs a positive integer 'max_attempts'{_offending(max_attempts)}"
                )


def _process_umask() -> int | None:
    """The process umask, or ``None`` when it cannot be read.

    :func:`os.umask` is a swap, not a getter, so the portable read is
    set-then-restore, which publishes a different mask to every other thread for
    the width of two calls. Linux exposes the value in ``/proc/self/status``
    (since 4.7), so prefer that and fall back to the swap. A missing or garbled
    ``Umask:`` line takes the fallback rather than raising: this is a permission
    hint, not the registry. The placeholder in the fallback is deliberately
    *narrower* than any plausible real umask, so if another thread does create a
    file inside that window it lands too private rather than world-writable.
    """
    try:
        with open(_UMASK_STATUS_PATH, encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    try:
        mask = os.umask(0o077)
        os.umask(mask)
    except OSError:  # pragma: no cover - os.umask exists on every supported platform
        return None
    return mask


class _StagedAttributes(NamedTuple):
    """What a replaced registry's inode carried and a replacement must re-establish.

    :func:`os.replace` moves a *new* inode into place, so nothing the old file
    carried survives on its own. Each field is ``None`` when there is nothing to
    reproduce or no way to learn it, which the caller reads as "leave the tighter
    default alone" rather than as a value to guess at.
    """

    mode: int | None
    uid: int | None
    gid: int | None


_NOTHING_TO_RESTORE = _StagedAttributes(None, None, None)


def _staged_attributes(path: Path) -> _StagedAttributes:
    """Permissions and ownership a rewritten registry should end up with.

    :func:`tempfile.mkstemp` creates its staging file 0600 under the saving
    process's own uid and gid, and :func:`os.replace` installs that inode as it
    is, so an atomic rewrite would quietly narrow a registry that
    :meth:`Path.write_text` left readable: overwriting preserved the mode, the
    owner and the group, because it reused the inode instead of replacing it.
    This registry is read by hooks, sidecars and CI steps that may run under
    another uid or gid, and #413 makes a write happen per claim attempt, so the
    first save would lock them out for the rest of the run. That is a worse
    failure than the truncation the staging file exists to prevent.

    An existing target's own bits win, because they are the operator's decision
    and resetting them on every save is the other half of the same bug. Only the
    permission bits are copied: setuid, setgid and sticky are not carried onto a
    file the saving process newly owns. Ownership is read only where it can be
    written back, so a platform without :func:`os.chown` reports ``None`` rather
    than a value nothing can act on.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        mask = _process_umask()
        if mask is None:
            return _NOTHING_TO_RESTORE
        # 0o666, not 0o644: that is the mode open() passes for a new text file, so
        # a first save reproduces what write_text produced under the same umask.
        # A file being created has no previous owner to put back.
        return _StagedAttributes(0o666 & ~mask, None, None)
    except OSError:
        return _NOTHING_TO_RESTORE
    if not _CAN_CHOWN:
        return _StagedAttributes(info.st_mode & 0o777, None, None)
    return _StagedAttributes(info.st_mode & 0o777, info.st_uid, info.st_gid)


def _restore_ownership(name: str, attributes: _StagedAttributes) -> None:
    """Put the replaced inode's owner and group onto the staged file.

    The mode is only half of an answer to "who may read this". A registry an
    operator set to ``0640`` with a shared group grants that group nothing once
    the group becomes whichever one the saving process happens to sit in, so the
    permission fix has to carry ownership too or it only looks complete.

    Best effort, like the chmod: a process that is not root cannot give a file
    away and cannot join a group it is not in, and the gid is attempted on its own
    when the pair is refused, because the group is the half that grants access to
    anyone else. Failing the save instead - as opposed to failing to reproduce a
    group - would turn an unreproducible permission into a refused claim, which is
    the wrong direction for a fail-closed gate: the replacement never had that
    group's access to hand out, so nothing is lost that this call could keep.
    """
    if sys.platform == "win32":  # pragma: no cover - one arm is dead on either platform
        # Named literally rather than through _CAN_CHOWN, which already keeps the
        # gid out of _staged_attributes here: os.chown does not exist on Windows,
        # so the calls below have to be unreachable to a type checker too.
        return
    if attributes.gid is None:
        return
    current = os.stat(name)
    if (attributes.uid, attributes.gid) == (current.st_uid, current.st_gid):
        return  # The common case: one process rewriting a registry it owns.
    with contextlib.suppress(OSError):
        os.chown(name, attributes.uid if attributes.uid is not None else -1, attributes.gid)
        return
    with contextlib.suppress(OSError):
        os.chown(name, -1, attributes.gid)


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry a preceding :func:`os.replace` created.

    Fsyncing the staged file commits its *contents*; the rename itself is a
    change to the parent directory, so without this a crash immediately after a
    successful save could still lose the new registry and leave the previous one
    in place. Nothing is corrupted by that - the old file is complete and
    loadable - but durability across power loss is the guarantee
    :func:`save_budgets` exists to make.

    Best effort. Windows cannot open a directory as a file descriptor, and some
    filesystems refuse ``fsync`` on one; both cases leave the write no less
    durable than it was before this call existed, so failing the save over it
    would trade a narrow durability gap for a refused claim.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def save_budgets(path: Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` back to the registry as readable JSON, atomically.

    Insertion order is preserved so editing one entry does not churn the whole
    file, and the trailing newline matches how hand-maintained registries end.
    Keys the loader does not know pass through untouched, exactly as when the
    file is edited by hand.

    The bytes land in a sibling temporary file that is flushed and fsynced, then
    moved over the target with :func:`os.replace`, which is atomic within a
    filesystem on both POSIX and Windows (issue #427). Writing in place would
    truncate first, so a crash, an OOM kill or power loss between truncation and
    flush left a zero-length or half-written registry; every later
    :func:`load_budgets` then raises, which is fail-closed, so a budget-gated
    claim refused until an operator repaired the file by hand. Losing the last
    increment on an abrupt exit is an acceptable price for a counter registry.
    Losing the registry is not, and #413 makes this a write per claim attempt.

    Staging replaces an inode rather than rewriting one, so three properties of
    the old file have to be re-established deliberately or the rewrite quietly
    changes something it was never asked to. Who may read it, owner and group
    included (:func:`_staged_attributes`, :func:`_restore_ownership`). Which file
    is written, when the registry is a symlink to a shared one: ``write_text``
    followed the link and ``os.replace`` would swap the link itself for a regular
    file, leaving every other consumer of the shared target on stale counters, so
    the path is resolved first. And whether the rename survives a crash
    (:func:`_fsync_directory`). A save that outlives a power cut but locks a hook
    out of the file, or writes past a symlink, has not delivered what the staging
    file was added for.
    """
    # Resolved, so a symlinked registry is written through rather than replaced,
    # matching both write_text and load_budgets, which reads through the link. It
    # also puts the staging file beside the *real* target, keeping the rename
    # inside one filesystem.
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=2) + "\n"
    # Read before the replace, while the target is still the file being replaced.
    attributes = _staged_attributes(path)
    # Same directory as the target: os.replace is only atomic within one
    # filesystem, and the system temp dir is routinely on another.
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp: Path | None = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        # Ownership before the mode: the bits mean nothing until they apply to the
        # identity they were set for, and chown clears setuid and setgid on some
        # systems, which chmod running second would silently undo.
        _restore_ownership(name, attributes)
        if attributes.mode is not None:
            # A filesystem without permission bits keeps mkstemp's 0600: too
            # private is recoverable by hand, a failed save is not.
            with contextlib.suppress(OSError):
                os.chmod(name, attributes.mode)
        os.replace(name, path)
        tmp = None  # Consumed by the replace; there is nothing left to clean up.
        _fsync_directory(path.parent)
    finally:
        if tmp is not None:
            # A failed save leaves the previous registry in place and no litter.
            tmp.unlink(missing_ok=True)


def _bound_entry(
    raw: Mapping[str, Any],
    action_type: str,
    authorization_id: str,
) -> dict[str, Any]:
    """The registry's entry for one authorization; KeyError names a missing one."""
    section = raw.get(AUTHORIZATION_BOUND_KEY)
    entries = section.get(action_type, {}) if isinstance(section, dict) else {}
    entry = entries.get(authorization_id) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        raise KeyError(f"no authorization-bound budget for {action_type!r} / {authorization_id!r}")
    return entry


def get_remaining(
    raw: Mapping[str, Any],
    action_type: str,
    authorization_id: str,
) -> int | None:
    """Attempts still available to this authorization, or ``None`` when unbound.

    Pure: it reads only the mapping :func:`load_budgets` returned. An absent
    section reads as unbound, which keeps runs without authorization data on
    today's behaviour (epic #390).
    """
    try:
        entry = _bound_entry(raw, action_type, authorization_id)
    except KeyError:
        return None
    counter = entry.get("counter", 0)
    maximum = entry["max_attempts"]

    if not _is_int(counter) or counter < 0:
        raise BudgetConfigError(
            f"authorization-bound budget needs a non-negative integer "
            f"'counter'{_offending(counter)}"
        )

    if not _is_int(maximum) or maximum < 0:
        raise BudgetConfigError(
            f"authorization-bound budget needs a positive integer "
            f"'max_attempts'{_offending(maximum)}"
        )

    return max(0, maximum - counter)


def increment(
    raw: Mapping[str, Any],
    action_type: str,
    authorization_id: str,
) -> int:
    """Count one more attempt against the authorization, return what remains.

    Mutates ``raw`` in place; the counter only ever climbs, and persisting the
    change is the caller's job (:func:`save_budgets`). An authorization the
    registry does not know raises KeyError rather than receiving an invented
    cap, because guessing a limit the operator never set is how budgets stop
    meaning anything.
    """
    entry = _bound_entry(raw, action_type, authorization_id)

    counter = entry.get("counter", 0)
    maximum = entry["max_attempts"]

    if not _is_int(counter) or counter < 0:
        raise BudgetConfigError(
            f"authorization-bound budget needs a non-negative integer "
            f"'counter'{_offending(counter)}"
        )

    if not _is_int(maximum) or maximum < 0:
        raise BudgetConfigError(
            f"authorization-bound budget needs a positive integer "
            f"'max_attempts'{_offending(maximum)}"
        )

    counter += 1
    entry["counter"] = counter
    return max(0, maximum - counter)


def would_refuse(
    raw: Mapping[str, Any],
    action_type: str,
    authorization_id: str,
) -> tuple[bool, str]:
    """Whether one more attempt under this authorization would be refused.

    Returns ``(refused, reason)``. Unbound authorizations never refuse here,
    matching today's behaviour; exhausted ones refuse with a reason naming the
    action type, the authorization id and both figures, so a caller can say
    exactly what ran out.
    """
    label = f"{action_type!r} / {authorization_id!r}"
    try:
        entry = _bound_entry(raw, action_type, authorization_id)
    except KeyError:
        return False, f"no authorization-bound budget for {label}"
    used = entry.get("counter", 0)
    maximum = entry["max_attempts"]

    if not _is_int(used) or used < 0:
        raise BudgetConfigError(
            f"authorization-bound budget needs a non-negative integer 'counter'{_offending(used)}"
        )

    if not _is_int(maximum) or maximum < 0:
        raise BudgetConfigError(
            f"authorization-bound budget needs a positive integer "
            f"'max_attempts'{_offending(maximum)}"
        )
    if used >= maximum:
        detail = f"{label} exhausted its authorization-bound budget"
        return True, f"{detail} ({used} of {maximum} attempts used)"
    return False, f"{label} has {maximum - used} of {maximum} attempts remaining"

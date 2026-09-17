"""Optional resource limits for recovery execution.

Recovery can execute user supplied probes and validators. A runaway probe
must not hang the caller that runs it. Limits are opt-in so existing runs
keep their current behavior when no limits are passed.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from typing import Any


class RecoveryTimeoutError(TimeoutError):
    """Raised when a recovery operation exceeds its allotted time."""


def run_with_limits(
    fn: Callable[..., Any],
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Run ``fn`` with an optional timeout.

    When ``timeout`` is None the function is called directly with no
    wrapping. When a timeout is given the call is executed in a worker thread
    and a ``RecoveryTimeoutError`` is raised if it does not complete in time.
    The caller can decide how to handle the timeout. Using a thread avoids
    signal restrictions on non main threads and works on all platforms.

    The deadline bounds when the caller gets control back, not when the worker
    stops: a probe that ignores its deadline cannot be killed, so it is left
    running in a detached thread and the exception is raised on time instead
    of after the runaway finishes.
    """
    if timeout is None:
        return fn(*args, **kwargs)

    if timeout <= 0:
        raise ValueError("timeout must be positive or None")

    # The executor is managed by hand rather than as a ``with`` block: leaving
    # the block runs ``shutdown(wait=True)``, which joins the worker. Python
    # cannot kill a thread, so a probe that ignores its deadline keeps running
    # either way, but joining it makes the timeout bound nothing: the caller
    # gets the exception only after the runaway finishes (issue #1147).
    # ``wait=False`` detaches the worker to die on its own, so the exception is
    # raised when the deadline passes and the caller is free to act on it.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise RecoveryTimeoutError(f"recovery operation timed out after {timeout}s") from exc
    finally:
        executor.shutdown(wait=False)

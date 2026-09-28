"""Optional resource limits for recovery execution.

Recovery can execute user supplied probes and validators. A runaway probe
must be terminated, not hung. Limits are opt-in so existing runs keep their
current behavior when no limits are passed.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from typing import Any

__all__ = [
    "RecoveryTimeoutError",
    "run_with_limits",
]


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

    On timeout the worker thread is not killed (Python cannot do that), it is
    detached. The exception reaches the caller at the deadline, not when the
    runaway happens to finish.
    """
    if timeout is None:
        return fn(*args, **kwargs)

    if timeout <= 0:
        raise ValueError("timeout must be positive or None")

    # The executor is never joined on timeout: Python cannot kill a thread, so
    # the runaway worker finishes (or hangs) on its own. Joining it through
    # ``shutdown(wait=True)`` would hand the caller the very hang the timeout
    # exists to bound.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise RecoveryTimeoutError(f"recovery operation timed out after {timeout}s") from exc
    finally:
        # wait=False: never join the worker, whether it finished, is still
        # running, or hung forever. cancel_futures=True drops any not-yet
        # started work; the running one is simply orphaned.
        executor.shutdown(wait=False, cancel_futures=True)

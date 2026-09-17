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
    """
    if timeout is None:
        return fn(*args, **kwargs)

    if timeout <= 0:
        raise ValueError("timeout must be positive or None")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise RecoveryTimeoutError(f"recovery operation timed out after {timeout}s") from exc

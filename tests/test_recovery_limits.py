import time

import pytest

from continuum.recovery import RecoveryTimeoutError, run_with_limits


def test_runaway_op_is_terminated_not_hung() -> None:
    def slow_op() -> str:
        time.sleep(0.5)
        return "done"

    with pytest.raises(RecoveryTimeoutError):
        run_with_limits(slow_op, timeout=0.05)


def test_timeout_bounds_when_the_caller_regains_control() -> None:
    """The deadline bounds the caller, not the worker (issue #1147).

    The worker cannot be killed, so it keeps sleeping either way, but the
    exception used to be raised only after leaving the ``with`` block, whose
    ``__exit__`` joins the worker. The caller got control back when the
    runaway finished, which makes the timeout bound nothing.
    """

    def slow_op() -> str:
        time.sleep(2.0)
        return "done"

    start = time.monotonic()
    with pytest.raises(RecoveryTimeoutError):
        run_with_limits(slow_op, timeout=0.1)
    elapsed = time.monotonic() - start
    # Well under the 2.0s the worker still sleeps for; generous enough for a
    # loaded CI box, tight enough that a join is caught.
    assert elapsed < 1.0, f"caller waited {elapsed:.2f}s for a 0.1s timeout"


def test_fast_call_then_slow_call_does_not_join_the_first() -> None:
    """``finally`` must detach too, or a slow second call hangs on the first.

    A successful fast call leaves its worker idle, but a ``wait=True``
    shutdown would still join anything the pool picks up, and a second call
    in the same interpreter can inherit a worker mid-run.
    """

    def fast_op() -> str:
        return "done"

    def slow_op() -> str:
        time.sleep(2.0)
        return "late"

    assert run_with_limits(fast_op, timeout=1.0) == "done"
    start = time.monotonic()
    with pytest.raises(RecoveryTimeoutError):
        run_with_limits(slow_op, timeout=0.1)
    assert time.monotonic() - start < 1.0


def test_limits_opt_in_no_timeout_runs_normally() -> None:
    def fast_op(x: int) -> int:
        return x * 2

    assert run_with_limits(fast_op, 21) == 42
    assert run_with_limits(fast_op, 21, timeout=None) == 42


def test_invalid_timeout_rejected() -> None:
    with pytest.raises(ValueError):
        run_with_limits(lambda: None, timeout=0)


def test_timed_call_returns_its_result_when_it_beats_the_deadline() -> None:
    def fast_op(x: int) -> int:
        return x * 2

    assert run_with_limits(fast_op, 21, timeout=5.0) == 42

import time

import pytest

from continuum.recovery import RecoveryTimeoutError, run_with_limits


def test_runaway_op_is_terminated_not_hung() -> None:
    def slow_op() -> str:
        time.sleep(0.5)
        return "done"

    with pytest.raises(RecoveryTimeoutError):
        run_with_limits(slow_op, timeout=0.05)


def test_timeout_bounds_when_caller_gets_control_back() -> None:
    # The bug this pins: the executor's __exit__ used to run
    # shutdown(wait=True), joining the runaway worker, so the caller saw the
    # RecoveryTimeoutError only after the probe finished. The exception must
    # arrive at the deadline, not at the probe's leisure.
    def slow_probe() -> str:
        time.sleep(3.0)
        return "done"

    start = time.monotonic()
    with pytest.raises(RecoveryTimeoutError):
        run_with_limits(slow_probe, timeout=0.1)
    assert time.monotonic() - start < 1.0


def test_success_path_does_not_join_worker() -> None:
    # A successful call returns the result and leaves no executor behind that
    # would block interpreter shutdown on later reuse.
    start = time.monotonic()
    assert run_with_limits(lambda: "ok", timeout=5.0) == "ok"
    assert time.monotonic() - start < 1.0


def test_limits_opt_in_no_timeout_runs_normally() -> None:
    def fast_op(x: int) -> int:
        return x * 2

    assert run_with_limits(fast_op, 21) == 42
    assert run_with_limits(fast_op, 21, timeout=None) == 42


def test_invalid_timeout_rejected() -> None:
    with pytest.raises(ValueError):
        run_with_limits(lambda: None, timeout=0)

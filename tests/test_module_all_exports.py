"""Tests for public module export surfaces (__all__) (issue #913)."""

from __future__ import annotations

import subprocess
import sys

import continuum.actions.ledger as action_ledger
import continuum.gate as gate
import continuum.pinning as pinning
import continuum.recovery.contract as recovery_contract
import continuum.recovery.gate as recovery_gate


def test_action_ledger_exports_fold_action_events() -> None:
    assert "fold_action_events" in action_ledger.__all__
    assert callable(action_ledger.fold_action_events)


def test_recovery_contract_exports_render_contract() -> None:
    assert "render_contract" in recovery_contract.__all__
    assert callable(recovery_contract.render_contract)


def test_gate_exports_gate_config_error() -> None:
    assert "GateConfigError" in gate.__all__
    assert issubclass(gate.GateConfigError, Exception)


def test_pinning_exports_latest_pinning() -> None:
    assert "latest_pinning" in pinning.__all__
    assert callable(pinning.latest_pinning)


def test_recovery_gate_exports_stamp_lineage() -> None:
    assert "stamp_lineage" in recovery_gate.__all__
    assert callable(recovery_gate.stamp_lineage)


def test_all_symbols_exist_on_modules() -> None:
    modules = [
        action_ledger,
        recovery_contract,
        gate,
        pinning,
        recovery_gate,
    ]
    for mod in modules:
        for name in mod.__all__:
            assert hasattr(mod, name), (
                f"{mod.__name__} missing attribute {name!r} listed in __all__"
            )


def test_star_import_execution() -> None:
    code = """
from continuum.actions.ledger import *
from continuum.recovery.contract import *
from continuum.gate import *
from continuum.pinning import *
from continuum.recovery.gate import *

assert callable(fold_action_events)
assert callable(render_contract)
assert issubclass(GateConfigError, Exception)
assert callable(latest_pinning)
assert callable(stamp_lineage)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"Star import failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

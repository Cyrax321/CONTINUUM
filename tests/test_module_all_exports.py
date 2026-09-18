"""Tests for public module export surfaces (__all__) (issue #913)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import continuum.actions.ledger as action_ledger
import continuum.adapters.actions as adapters_actions
import continuum.adapters.browser as adapters_browser
import continuum.adapters.container as adapters_container
import continuum.adapters.filesystem as adapters_filesystem
import continuum.adapters.kubernetes as adapters_kubernetes
import continuum.adapters.python_inproc as adapters_python_inproc
import continuum.adapters.registry as adapters_registry
import continuum.analysis as analysis
import continuum.analysis.depends as analysis_depends
import continuum.benchmark.baselines as benchmark_baselines
import continuum.benchmark.controlled_failures as benchmark_controlled_failures
import continuum.benchmark.phase6.harness as phase6_harness
import continuum.benchmark.phase6.metrics as phase6_metrics
import continuum.benchmark.phase6.scenarios as phase6_scenarios
import continuum.dashboard.app as dashboard_app
import continuum.dashboard.hitl as dashboard_hitl
import continuum.gate as gate
import continuum.hooks as hooks
import continuum.pinning as pinning
import continuum.plugins.registry as plugins_registry
import continuum.reconcilers as reconcilers
import continuum.recovery.cleanup as recovery_cleanup
import continuum.recovery.contract as recovery_contract
import continuum.recovery.gate as recovery_gate
import continuum.recovery.impact as recovery_impact
import continuum.recovery.limits as recovery_limits
import continuum.recovery.notify as recovery_notify
import continuum.replayguard as replayguard
import continuum.security.provenance as security_provenance
import continuum.security.revalidation as security_revalidation
import continuum.serve as serve
import continuum.serve.server as serve_server
import continuum.storage.postgres as storage_postgres
import continuum.testing.fixtures as testing_fixtures


def _locally_defined_public_names(mod: ModuleType) -> set[str]:
    """Public, underscore-free names a module defines itself.

    Imported names are excluded (they belong to the module that defines them)
    and so are TypeVars, mirroring `state/diff.py` keeping `T` out of its list.
    """
    tree = ast.parse(Path(mod.__file__ or "").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            is_typevar = isinstance(node.value, ast.Call) and getattr(
                node.value.func, "id", ""
            ) in {"TypeVar", "ParamSpec", "TypeVarTuple"}
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and not target.id.startswith("_")
                    and not is_typevar
                ):
                    names.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and not node.target.id.startswith("_")
        ):
            names.add(node.target.id)
    return names


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
        adapters_actions,
        adapters_browser,
        adapters_container,
        adapters_filesystem,
        adapters_kubernetes,
        adapters_python_inproc,
        adapters_registry,
        analysis_depends,
        benchmark_baselines,
        benchmark_controlled_failures,
        phase6_harness,
        phase6_metrics,
        phase6_scenarios,
        dashboard_app,
        hooks,
        plugins_registry,
        recovery_cleanup,
        recovery_impact,
        recovery_limits,
        recovery_notify,
        replayguard,
        dashboard_hitl,
        reconcilers,
        serve_server,
        security_provenance,
        security_revalidation,
        storage_postgres,
        testing_fixtures,
    ]
    for mod in modules:
        for name in mod.__all__:
            assert hasattr(mod, name), (
                f"{mod.__name__} missing attribute {name!r} listed in __all__"
            )
        # Dropping a name from __all__ would silently shrink the loop above, so
        # pin __all__ against the module's own definitions rather than itself.
        assert set(mod.__all__) == _locally_defined_public_names(mod), (
            f"{mod.__name__}.__all__ does not match the public names it defines"
        )


def test_star_import_execution() -> None:
    code = """
from continuum.actions.ledger import *
from continuum.recovery.contract import *
from continuum.gate import *
from continuum.pinning import *
from continuum.recovery.gate import *
from continuum.adapters.actions import *
from continuum.adapters.browser import *
from continuum.adapters.container import *
from continuum.adapters.filesystem import *
from continuum.adapters.kubernetes import *
from continuum.adapters.python_inproc import *
from continuum.adapters.registry import *
from continuum.analysis import *
from continuum.analysis.depends import *
from continuum.benchmark.baselines import *
from continuum.benchmark.controlled_failures import *
from continuum.benchmark.phase6.harness import *
from continuum.benchmark.phase6.metrics import *
from continuum.benchmark.phase6.scenarios import *
from continuum.dashboard.app import *
from continuum.dashboard.hitl import *
from continuum.hooks import *
from continuum.plugins.registry import *
from continuum.reconcilers import *
from continuum.recovery.cleanup import *
from continuum.recovery.impact import *
from continuum.recovery.limits import *
from continuum.recovery.notify import *
from continuum.replayguard import *
from continuum.security.provenance import *
from continuum.security.revalidation import *
from continuum.serve import *
from continuum.serve.server import *
from continuum.storage.postgres import *
from continuum.testing.fixtures import *

assert callable(fold_action_events)
assert callable(render_contract)
assert issubclass(GateConfigError, Exception)
assert callable(latest_pinning)
assert callable(stamp_lineage)
assert callable(get_adapter)
assert callable(baseline_by_name)
assert issubclass(RecoveryTimeoutError, Exception)
assert callable(run_revalidation)
assert callable(make_auto_checkpoint_hook)
assert issubclass(ReplayBlocked, Exception)
assert issubclass(HitlUnauthorized, Exception)
assert isinstance(SettleReport, type)
assert isinstance(SidecarHTTP, type)
assert isinstance(SubprocessClient, type)
assert isinstance(DependencyGraph, type)
assert AGENT_SOURCE is not None
assert MAX_SIDECAR_BODY_BYTES > 0
assert SIDECAR_DRAIN_LIMIT_BYTES > 0
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"Star import failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_adapters_registry_exports_get_adapter() -> None:
    assert "get_adapter" in adapters_registry.__all__
    assert callable(adapters_registry.get_adapter)


def test_benchmark_baselines_exports_baseline_by_name() -> None:
    assert "baseline_by_name" in benchmark_baselines.__all__
    assert callable(benchmark_baselines.baseline_by_name)


def test_recovery_limits_exports_recovery_timeout_error() -> None:
    assert "RecoveryTimeoutError" in recovery_limits.__all__
    assert issubclass(recovery_limits.RecoveryTimeoutError, Exception)


def test_security_revalidation_exports_run_revalidation() -> None:
    assert "run_revalidation" in security_revalidation.__all__
    assert callable(security_revalidation.run_revalidation)


def test_hooks_exports_make_auto_checkpoint_hook() -> None:
    assert "make_auto_checkpoint_hook" in hooks.__all__
    assert callable(hooks.make_auto_checkpoint_hook)


def test_replayguard_exports_replay_blocked() -> None:
    assert "ReplayBlocked" in replayguard.__all__
    assert issubclass(replayguard.ReplayBlocked, RuntimeError)


def test_dashboard_hitl_exports_hitl_unauthorized() -> None:
    assert "HitlUnauthorized" in dashboard_hitl.__all__
    assert issubclass(dashboard_hitl.HitlUnauthorized, Exception)


def test_reconcilers_exports_settle_report() -> None:
    assert "SettleReport" in reconcilers.__all__
    assert isinstance(reconcilers.SettleReport, type)


def test_serve_server_exports_sidecar_http_and_constants() -> None:
    for name in (
        "SidecarHTTP",
        "AGENT_SOURCE",
        "MAX_SIDECAR_BODY_BYTES",
        "SIDECAR_DRAIN_LIMIT_BYTES",
    ):
        assert name in serve_server.__all__
    assert isinstance(serve_server.SidecarHTTP, type)


def test_serve_exports_subprocess_client() -> None:
    assert "SubprocessClient" in serve.__all__
    assert isinstance(serve.SubprocessClient, type)


def test_analysis_exports_no_private_names() -> None:
    assert "_normalize_dep" not in analysis.__all__
    assert "_STDLIB" not in analysis.__all__
    assert "DependencyGraph" in analysis.__all__

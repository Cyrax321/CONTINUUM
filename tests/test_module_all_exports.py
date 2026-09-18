"""Tests for public module export surfaces (__all__) (issues #913, #1228)."""

from __future__ import annotations

import ast
import importlib
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import continuum
import continuum.actions.ledger as action_ledger
import continuum.adapters.actions as adapters_actions
import continuum.adapters.browser as adapters_browser
import continuum.adapters.container as adapters_container
import continuum.adapters.filesystem as adapters_filesystem
import continuum.adapters.kubernetes as adapters_kubernetes
import continuum.adapters.python_inproc as adapters_python_inproc
import continuum.adapters.registry as adapters_registry
import continuum.analysis.depends as analysis_depends
import continuum.benchmark.baselines as benchmark_baselines
import continuum.benchmark.controlled_failures as benchmark_controlled_failures
import continuum.benchmark.phase6.harness as phase6_harness
import continuum.benchmark.phase6.metrics as phase6_metrics
import continuum.benchmark.phase6.scenarios as phase6_scenarios
import continuum.dashboard.app as dashboard_app
import continuum.gate as gate
import continuum.hooks as hooks
import continuum.pinning as pinning
import continuum.plugins.registry as plugins_registry
import continuum.recovery.cleanup as recovery_cleanup
import continuum.recovery.contract as recovery_contract
import continuum.recovery.gate as recovery_gate
import continuum.recovery.impact as recovery_impact
import continuum.recovery.limits as recovery_limits
import continuum.recovery.notify as recovery_notify
import continuum.security.provenance as security_provenance
import continuum.security.revalidation as security_revalidation
import continuum.storage.postgres as storage_postgres
import continuum.testing.fixtures as testing_fixtures


def _modules_with_all() -> list[ModuleType]:
    """Every importable module in the package that declares an ``__all__``.

    The guard used to enumerate five modules by hand, so 93 modules' export
    lists went unchecked (#1228); a rename that forgot ``__all__`` went green
    everywhere it mattered. Modules whose import fails on a missing optional
    extra are skipped rather than erroring the guard, and ``continuum.__main__``
    is excluded because importing it has a side effect.
    """
    found: list[ModuleType] = []
    for info in pkgutil.walk_packages(continuum.__path__, "continuum."):
        if info.name == "continuum.__main__":
            continue
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue
        if getattr(module, "__all__", None):
            found.append(module)
    return found


#: Leaf modules whose ``__all__`` is exactly the public surface they define.
#: Aggregator modules (``continuum.actions``, ``continuum.cli``) re-export
#: names from submodules and are excluded, as are modules holding a public
#: name back from ``__all__`` on purpose (``continuum.budgets.FALLBACK_MAX_ATTEMPTS``).
_LEAF_MODULES_WITH_COMPLETE_ALL = [
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
    security_provenance,
    security_revalidation,
    storage_postgres,
    testing_fixtures,
]


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
    """Every name in every module's ``__all__`` resolves at import time.

    Walks the installed package rather than a hand-rolled list, so a refactor
    that renames a symbol and forgets ``__all__`` fails on all 122 modules
    instead of the 5 the old list covered (#1228).
    """
    modules = _modules_with_all()
    # The walk is the whole point: if it silently shrank to nothing the test
    # would pass vacuously. Pin a floor well below today's count so a change
    # that breaks package discovery is caught, not absorbed.
    assert len(modules) >= 90, f"walk found only {len(modules)} modules with __all__"
    for mod in modules:
        for name in mod.__all__:
            assert hasattr(mod, name), (
                f"{mod.__name__} missing attribute {name!r} listed in __all__"
            )


def test_all_matches_locally_defined_public_names() -> None:
    """On leaf modules, ``__all__`` is exactly the public surface it defines.

    This cannot hold package-wide, so the list stays curated: aggregator
    modules (``continuum.actions`` re-exports all 15 of its entries from
    submodules) legitimately list names they do not define, and some modules
    hold a public name back from ``__all__`` on purpose (``continuum.budgets``
    keeps ``FALLBACK_MAX_ATTEMPTS`` private to its own defaulting). Both are
    per-module policy, so only modules that define their whole surface are
    pinned here. Dropping a name from ``__all__`` would silently shrink the
    assertion, which is why the check is against the definitions, not itself.
    """
    for mod in _LEAF_MODULES_WITH_COMPLETE_ALL:
        assert set(mod.__all__) == _locally_defined_public_names(mod), (
            f"{mod.__name__}.__all__ does not match the public names it defines"
        )


def test_star_import_execution() -> None:
    """A star import of every public module executes without error.

    This used to list the 29 modules above by hand, so a module whose import
    had a side effect -- or a name that collided and shadowed an earlier
    import -- went unnoticed (#1228). The walk covers all of them, and the
    assertions after it stay because a walk that shrank to nothing would
    otherwise leave the subprocess importing nothing at all.
    """
    code = "\n".join(f"from {mod.__name__} import *" for mod in _modules_with_all())
    code += """
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

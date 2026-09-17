"""Tests for public module export surfaces (__all__) (issue #913).

The guard was extended in #1228: it enumerated five modules by hand, and 98
modules declare an ``__all__`` today, so 93 of them got no check at all. A
refactor that renames a function and forgets ``__all__``, or lists a name that
no longer exists, went green on 93 modules and red on 5 -- the shape of drift
the #903 / #913 / #1093 / #1111 series kept finding by hand after the fact.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
import types

import continuum
import continuum.actions.ledger as action_ledger
import continuum.gate as gate
import continuum.pinning as pinning
import continuum.recovery.contract as recovery_contract
import continuum.recovery.gate as recovery_gate

#: The five modules the named tests below pin. They stay hand-rolled on
#: purpose: they document *intent* -- which symbols a reader is meant to rely on
#: -- which a sweep over every module cannot express.
NAMED = (action_ledger, recovery_contract, gate, pinning, recovery_gate)


def _modules_with_all() -> list[types.ModuleType]:
    """Every importable module in the package that declares an ``__all__``.

    ``continuum.__main__`` is skipped because importing it has a side effect: it
    runs the CLI. Modules behind an optional extra (``otel``, ``postgres``) are
    skipped when the extra is absent, because a module that cannot be imported
    cannot be checked and a missing dependency is not an export defect. Any
    *other* import failure still propagates, so a genuinely broken module fails
    the suite instead of being silently exempted.
    """
    out: list[types.ModuleType] = []
    for info in pkgutil.walk_packages(continuum.__path__, "continuum."):
        if info.name == "continuum.__main__":
            continue
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue
        if hasattr(module, "__all__"):
            out.append(module)
    return out


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


def test_all_symbols_exist_on_their_module() -> None:
    """Every name a module's ``__all__`` advertises resolves to a real attribute.

    This is the check that was covering 5 of 98 modules: it is what catches a
    rename that leaves ``__all__`` pointing at a name that no longer exists.
    Failures are aggregated rather than raised per-module, because the first one
    is rarely the only one.
    """
    missing = [
        f"{module.__name__}.{name}"
        for module in _modules_with_all()
        for name in module.__all__
        if not hasattr(module, name)
    ]
    assert not missing, f"names listed in __all__ that their module does not define: {missing}"


def test_all_entries_are_strings() -> None:
    """``from module import *`` reads ``__all__`` as a sequence of names.

    A non-string entry -- an integer, a type, a stray ``None`` -- is a typo that
    turns every star-import of that module into a ``TypeError``, so it is worth
    catching here rather than at the first caller that tries it. A ``tuple`` is
    accepted alongside a ``list``: both are legal Python, and the guard checks
    the names, not the container.
    """
    bad = []
    for module in _modules_with_all():
        exported = module.__all__
        if not isinstance(exported, (list, tuple)) or not all(
            isinstance(name, str) for name in exported
        ):
            bad.append(module.__name__)
    assert not bad, f"modules whose __all__ is not a list/tuple of strings: {bad}"


def test_every_module_is_covered() -> None:
    """The sweep found something to check. A walk that returns nothing -- say,
    because ``pkgutil`` stopped recursing -- would make the two tests above
    vacuously pass, which is worse than not having them."""
    found = _modules_with_all()
    assert len(found) >= len(NAMED), (
        f"walk found {len(found)} modules, expected at least {len(NAMED)}"
    )
    assert all(module in found for module in NAMED)


def test_star_import_execution() -> None:
    """Every module star-imports cleanly in a fresh interpreter.

    Stronger than the attribute check above because it runs the actual
    ``from module import *`` statement against every ``__all__`` at once: a name
    listed but undefined raises ``AttributeError`` here too, and a module whose
    import has a side effect that a bare ``importlib.import_module`` tolerates
    surfaces in a subprocess that cannot. The set is the importable modules the
    guard itself walked, so an environment without an optional extra still exits
    0 rather than importing something it does not have.
    """
    code = "\n".join(f"from {module.__name__} import *" for module in _modules_with_all())
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"Star import failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )

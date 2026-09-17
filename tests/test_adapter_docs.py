"""The documented adapter constructors must be the ones the classes accept.

``docs/api/adapters.md`` teaches one constructor line per adapter, and a reader
who copies it expects it to run. Three of those lines documented an ``engine=``
keyword that LangGraph, OpenAI and LangChain reject outright -- the documented
call raised ``TypeError`` naming a keyword the page had just told the reader to
pass (issue #1230). The page ``__all__``-exports these classes, so the signature
is part of the promised surface rather than an implementation detail.

The audit is a test rather than a periodic reread for the same reason
``test_mcp_docs`` is: the drift is silent, and ``inspect.signature`` is the only
authority on it.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path

import pytest

from continuum.adapters import GenericAgentAdapter

#: The page whose constructor lines mirror the class signatures.
ADAPTERS_DOC = Path(__file__).resolve().parents[1] / "docs" / "api" / "adapters.md"

#: A documented constructor: the dotted class path and the argument list inside
#: the parens. ``python_inproc`` is a nested module, so the path may carry a
#: second dot below ``continuum.adapters``.
SIGNATURE_LINE = re.compile(r"^`(?P<path>continuum\.adapters\.[\w.]+)\((?P<args>[^`]*)\)`$")

#: Registry and helper classes on the page are not adapters; only the classes a
#: reader constructs against a storage get a signature check.
ADAPTER_SUFFIX = "Adapter"


def _documented_constructors() -> list[tuple[str, str]]:
    """Every ```path(args)``` `` line in the page, as (path, args)."""
    out: list[tuple[str, str]] = []
    for line in ADAPTERS_DOC.read_text(encoding="utf-8").splitlines():
        match = SIGNATURE_LINE.match(line.strip())
        if match and match["path"].rsplit(".", 1)[-1].endswith(ADAPTER_SUFFIX):
            out.append((match["path"], match["args"]))
    return out


def _split_args(args: str) -> tuple[list[str], list[str], dict[str, object]]:
    """Split a documented argument list into (positional, keyword) names and the
    documented defaults.

    ``storage, *, engine=None, auto_file=None`` yields ``(["storage"],
    ["engine", "auto_file"], {"engine": None, "auto_file": None})``. What makes
    a parameter keyword-only is the ``*`` that precedes it, not the ``=``.
    Defaults are parsed with :func:`ast.literal_eval` so the source spelling
    does not matter: ``namespace="default"`` compares as the string
    ``"default"`` and would also have matched ``'default'``.
    """
    positional: list[str] = []
    keyword: list[str] = []
    defaults: dict[str, object] = {}
    seen_star = False
    for token in (t.strip() for t in args.split(",")):
        if not token:
            continue
        if token == "*":
            seen_star = True
            continue
        name, sep, raw_default = token.partition("=")
        name = name.strip()
        (keyword if seen_star else positional).append(name)
        if sep:
            defaults[name] = ast.literal_eval(raw_default.strip())
    return positional, keyword, defaults


def _resolve(path: str) -> type:
    """Import the class a documented constructor line names.

    The page writes package-level paths (``continuum.adapters.BrowserAdapter``)
    for everything except ``python_inproc``, which it writes by module. The
    package re-exports the former, so both resolve without guessing a module
    name from the class name.
    """
    module_path, _, class_name = path.rpartition(".")
    if module_path == "continuum.adapters":
        return getattr(importlib.import_module("continuum.adapters"), class_name)
    return getattr(importlib.import_module(module_path), class_name)


def test_the_page_yields_parsable_constructor_lines() -> None:
    """A reformatted page that matches no line empties the parametrize set.

    ``_documented_constructors`` builds the cases at *collection* time, so a
    page whose reformatting matches ``SIGNATURE_LINE`` nowhere reports the
    signature guard as *skipped* rather than *failed* -- the guard would stop
    enforcing with no red test. Parsing itself has to be audited.
    """
    assert _documented_constructors(), (
        f"no constructor line in {ADAPTERS_DOC} matched {SIGNATURE_LINE.pattern}"
    )


@pytest.mark.parametrize("path, args", _documented_constructors())
def test_documented_constructor_matches_the_class(path: str, args: str) -> None:
    """Every documented parameter exists on the class, none is missing, and the
    documented defaults are the defaults the class actually applies."""
    documented_positional, documented_keyword, documented_defaults = _split_args(args)
    parameters = inspect.signature(_resolve(path).__init__).parameters
    real_positional = [
        name
        for name, param in parameters.items()
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and name != "self"
    ]
    real_keyword = [
        name for name, param in parameters.items() if param.kind is inspect.Parameter.KEYWORD_ONLY
    ]
    real_defaults = {
        name: param.default
        for name, param in parameters.items()
        if param.default is not inspect.Parameter.empty
    }
    assert documented_positional == real_positional, (
        f"{path}: documented positional {documented_positional} but the class "
        f"accepts {real_positional}"
    )
    assert sorted(documented_keyword) == sorted(real_keyword), (
        f"{path}: documented keyword {sorted(documented_keyword)} but the class "
        f"accepts {sorted(real_keyword)}"
    )
    assert documented_defaults == real_defaults, (
        f"{path}: documented defaults {documented_defaults} but the class applies {real_defaults}"
    )


def _public_adapters(package: object) -> set[type]:
    """Every concrete ``GenericAgentAdapter`` subclass the package exports.

    The framework adapters are reached through module ``__getattr__`` (PEP 562)
    and are cached in ``globals()`` only after first access, so ``vars()``
    sees only the two eagerly imported adapters and would silently excuse the
    six lazy ones. ``__all__`` is the public surface, and ``getattr`` resolves a
    lazy name exactly the way a reader's
    ``from continuum.adapters import BrowserAdapter`` does.
    """
    public: set[type] = set()
    for name in getattr(package, "__all__", ()):
        obj = getattr(package, name)
        if (
            inspect.isclass(obj)
            and issubclass(obj, GenericAgentAdapter)
            and obj is not GenericAgentAdapter
        ):
            public.add(obj)
    return public


def test_the_page_documents_every_public_adapter() -> None:
    """An exported adapter without a constructor line is a page a reader cannot
    use for it, which is how the three wrong lines went unnoticed.

    Compared by class, not by path string: the page writes ``python_inproc`` by
    module and the rest by package, so the same class appears under two
    spellings.
    """
    package = importlib.import_module("continuum.adapters")
    documented = {
        obj
        for path, _ in _documented_constructors()
        for obj in (_resolve(path),)
        if inspect.isclass(obj)
    }
    missing = sorted(obj.__name__ for obj in _public_adapters(package) - documented)
    assert not missing, f"adapters.md has no constructor line for: {missing}"

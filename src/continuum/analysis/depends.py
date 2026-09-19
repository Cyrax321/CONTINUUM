"""Source-level dependency ownership for localized recovery.

Phase 1 only knows whether an operation touched files. Real localized repair
needs to know which *dependency* a file (and therefore an operation on it)
belongs to, so the agent can repair that subtree instead of discarding the whole
bundle.

This module is deliberately dependency-light: it reads ``pyproject.toml``
(via the stdlib ``tomllib``, Python 3.11+) or ``requirements.txt`` for declared
dependencies, and parses ``import`` / ``from`` statements with the stdlib ``ast``
module. It does no execution and adds no third-party dependency, so it is safe
to run inside the agent loop.

The result is two queries used by recovery scoping:

* :meth:`DependencyGraph.owner_of` -- given a ``.py`` file, the set of declared
  dependencies it imports.
* :meth:`DependencyGraph.files_using` -- given a dependency, the ``.py`` files
  that import it.

When no dependency manifest is present, or parsing fails, the graph degrades
gracefully: it simply reports no declared owners rather than raising, so recovery
can fall back to whole-state behavior. See issues #100 and #109.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "DependencyGraph",
]

_STDLIB = set(sys.stdlib_module_names)

_EXCLUDE_DIRS = (".venv", "venv", "node_modules", "__pycache__", ".git", "build", "dist")


def _top_level(name: str) -> str:
    return name.split(".")[0]


def _normalize_dep(spec: str) -> str:
    """Reduce a dependency specifier to its bare, lower-cased name.

    ``numpy>=1.0``, ``Pillow[extra]==10.0``, ``foo ; python_version>\"3.8\"`` all
    collapse to ``numpy`` / ``pillow`` / ``foo``.
    """
    name = spec.split(";")[0].strip()
    for marker in ("[", "==", ">=", "<=", "!=", "~=", ">", "<", "=", " @ "):
        name = name.split(marker)[0]
    return name.strip().lower()


class DependencyGraph:
    """Maps ``.py`` files to the declared dependencies they import."""

    def __init__(
        self,
        root: str | Path,
        *,
        requirements: Iterable[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.declared: set[str] = self._read_declared(requirements)
        self._file_imports: dict[Path, set[str]] = {}
        self._package_files: dict[str, set[Path]] = {}
        self._scan()

    def _read_declared(self, requirements: Iterable[str] | None) -> set[str]:
        declared: set[str] = set()
        if requirements is not None:
            declared.update(_normalize_dep(r) for r in requirements if r.strip())
            return declared
        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            try:
                import tomllib

                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                deps = (data.get("project", {}) or {}).get("dependencies", []) or []
                declared.update(_normalize_dep(d) for d in deps if isinstance(d, str))
            except (OSError, ValueError):
                pass
        req = self.root / "requirements.txt"
        if req.is_file():
            for line in req.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                declared.add(_normalize_dep(line))
        return declared

    def _scan(self) -> None:
        try:
            paths = list(self.root.rglob("*.py"))
        except OSError:
            return
        for path in paths:
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mods.add(_top_level(alias.name))
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods.add(_top_level(node.module))
            self._file_imports[path] = mods
            for mod in mods:
                self._package_files.setdefault(mod, set()).add(path)

    @staticmethod
    def is_stdlib(name: str) -> bool:
        """Whether ``name`` is a Python standard-library module."""
        return _top_level(name) in _STDLIB

    def owner_of(self, file: str | Path) -> set[str]:
        """Declared dependencies imported by ``file`` (empty if none)."""
        mods = self._file_imports.get(Path(file), set())
        return {pkg for pkg in self.declared if _top_level(pkg) in mods}

    def files_using(self, package: str) -> set[Path]:
        """``.py`` files that import the top-level module of ``package``."""
        return set(self._package_files.get(_top_level(package), set()))

    def third_party_imports(self, file: str | Path) -> set[str]:
        """Top-level imports of ``file`` that are neither stdlib nor declared."""
        mods = self._file_imports.get(Path(file), set())
        return {m for m in mods if m not in _STDLIB and m not in self.declared}

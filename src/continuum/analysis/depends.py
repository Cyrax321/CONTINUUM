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
import functools
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

__all__ = [
    "DependencyGraph",
]

_STDLIB = {name.lower() for name in sys.stdlib_module_names}

_EXCLUDE_DIRS = (".venv", "venv", "node_modules", "__pycache__", ".git", "build", "dist")


def _canonical_dist(name: str) -> str:
    """Canonicalize a distribution name per PEP 503: lower-cased, [-_.]+ collapsed to '-'."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


_COMMON_DIST_TO_IMPORTS: dict[str, set[str]] = {
    "pillow": {"pil"},
    "pyyaml": {"yaml"},
    "beautifulsoup4": {"bs4"},
    "scikit-learn": {"sklearn"},
    "opencv-python": {"cv2"},
    "opencv-python-headless": {"cv2"},
    "python-dateutil": {"dateutil"},
    "attrs": {"attr", "attrs"},
    "protobuf": {"google.protobuf"},
    "setuptools": {"pkg_resources", "setuptools"},
    "pydantic-core": {"pydantic_core"},
    "typing-extensions": {"typing_extensions"},
}

_COMMON_IMPORT_TO_DISTS: dict[str, set[str]] = {}
for _dist, _imports in _COMMON_DIST_TO_IMPORTS.items():
    _dist_canon = _canonical_dist(_dist)
    for _imp in _imports:
        _COMMON_IMPORT_TO_DISTS.setdefault(_imp, set()).add(_dist_canon)


@functools.lru_cache(maxsize=1)
def _installed_packages_distributions() -> Mapping[str, list[str]]:
    """Installed top-level import name to distribution names mapping."""
    try:
        import importlib.metadata

        return importlib.metadata.packages_distributions()
    except Exception:
        return {}


@functools.lru_cache(maxsize=1)
def _installed_dist_to_imports() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for imp, dists in _installed_packages_distributions().items():
        imp_lower = imp.strip().lower()
        if imp_lower == "google":
            continue
        for d in dists:
            d_canon = _canonical_dist(d)
            out.setdefault(d_canon, set()).add(imp_lower)
    return out


@functools.lru_cache(maxsize=1)
def _installed_import_to_dists() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for imp, dists in _installed_packages_distributions().items():
        imp_lower = imp.strip().lower()
        if imp_lower == "google":
            continue
        for d in dists:
            out.setdefault(imp_lower, set()).add(_canonical_dist(d))
    return out


def _top_level(name: str) -> str:
    name = name.strip().lower()
    parts = name.split(".")
    if parts[0] == "google" and len(parts) > 1:
        return f"{parts[0]}.{parts[1]}"
    return parts[0]


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
        aliases: Mapping[str, str | Iterable[str]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.declared: set[str] = self._read_declared(requirements)
        self._custom_aliases: dict[str, set[str]] = self._parse_aliases(aliases)
        self._declared_candidates: dict[str, set[str]] = {
            pkg: self._candidate_imports(pkg) for pkg in self.declared
        }
        self._all_declared_imports_cache: set[str] = (
            set().union(*self._declared_candidates.values()) if self._declared_candidates else set()
        )
        self._file_imports: dict[Path, set[str]] = {}
        self._package_files: dict[str, set[Path]] = {}
        self._scan()

    @staticmethod
    def _parse_aliases(
        aliases: Mapping[str, str | Iterable[str]] | None,
    ) -> dict[str, set[str]]:
        if not aliases:
            return {}
        result: dict[str, set[str]] = {}
        for k, v in aliases.items():
            k_canon = _canonical_dist(k)
            k_norm = _normalize_dep(k)
            vals = {_top_level(v)} if isinstance(v, str) else {_top_level(x) for x in v}
            for key in (k_canon, k_norm):
                result.setdefault(key, set()).update(vals)
            for val in vals:
                val_canon = _canonical_dist(val)
                result.setdefault(val_canon, set()).add(k_canon)
                result.setdefault(val, set()).add(k_canon)
        return result

    def _candidate_imports(self, dep_or_mod: str) -> set[str]:
        norm = _normalize_dep(dep_or_mod)
        canon_dist = _canonical_dist(norm)
        top = _top_level(norm)
        names = {norm, canon_dist, top, top.replace("-", "_"), canon_dist.replace("-", "_")}
        if canon_dist.startswith("google-"):
            parts = canon_dist.split("-")
            if len(parts) > 1 and parts[1]:
                names.add(f"google.{parts[1].replace('-', '_')}")
        for key in (canon_dist, norm, top):
            if key in self._custom_aliases:
                names.update(self._custom_aliases[key])
        for key in (canon_dist, norm):
            if key in _COMMON_DIST_TO_IMPORTS:
                names.update(_COMMON_DIST_TO_IMPORTS[key])
        for key in (norm, top):
            if key in _COMMON_IMPORT_TO_DISTS:
                names.update(_COMMON_IMPORT_TO_DISTS[key])
        dist_map = _installed_dist_to_imports()
        for key in (canon_dist, norm):
            if key in dist_map:
                names.update(dist_map[key])
        imp_map = _installed_import_to_dists()
        for key in (norm, top):
            if key in imp_map:
                names.update(imp_map[key])
        return names

    def _all_declared_imports(self) -> set[str]:
        return set(self._all_declared_imports_cache)

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
            mods: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mods.add(_top_level(alias.name))
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mod_parts = node.module.strip().lower().split(".")
                    if mod_parts[0] == "google" and len(mod_parts) == 1:
                        for alias in node.names:
                            mods.add(f"google.{alias.name.strip().lower().split('.')[0]}")
                    else:
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
        target = Path(file)
        mods = self._file_imports.get(target)
        if mods is None:
            mods = self._file_imports.get(target.resolve())
        if mods is None and not target.is_absolute():
            mods = self._file_imports.get(self.root / target)
        if not mods:
            return set()
        return {pkg for pkg, cands in self._declared_candidates.items() if bool(cands & mods)}

    def files_using(self, package: str) -> set[Path]:
        """``.py`` files that import the top-level module of ``package``."""
        candidates = self._candidate_imports(package)
        candidates.add(_top_level(package))
        files: set[Path] = set()
        for mod in candidates:
            files.update(self._package_files.get(mod, ()))
        return files

    def third_party_imports(self, file: str | Path) -> set[str]:
        """Top-level imports of ``file`` that are neither stdlib nor declared."""
        target = Path(file)
        mods = self._file_imports.get(target)
        if mods is None:
            mods = self._file_imports.get(target.resolve())
        if mods is None and not target.is_absolute():
            mods = self._file_imports.get(self.root / target)
        if not mods:
            return set()
        declared_imports = self._all_declared_imports()
        return {m for m in mods if not self.is_stdlib(m) and m not in declared_imports}

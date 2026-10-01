"""Guard the code pointers in docs/GLOSSARY.md against drift (#1069).

The glossary pins each definition to a ``src/.../file.py:LINE`` pointer so a
reader can verify a claim in the code rather than infer it. Those pointers
drifted twice (#731, then #1069 found 16 of 25 stale) because nothing checked
them: a class moves and its citation keeps pointing at the line that used to
hold it. This module resolves every pointer against the source and fails the
suite the moment a citation no longer names the symbol its entry describes.

The expected table maps ``(file, line) -> (symbol, kind)`` where ``kind`` is
how the source declares the symbol: ``decl`` for ``class``/``def``, ``assign``
for a module- or body-level assignment or annotated field, ``ref`` for a
bare occurrence such as a call site. Line numbers live only in the table and
in the markdown, never in the resolver, so moving code does not rot the test:
the resolver re-finds the symbol and the glossary's number is what moves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "docs" / "GLOSSARY.md"
# A pointer may sit a few lines from the declaration (a decorator, a blank
# line, an argument list) but not tens of them: 16 of 25 pointers were off by
# 8 to 76 lines in #1069, and every one of those clears this tolerance only by
# landing on the wrong symbol.
TOLERANCE = 3

_POINTER_RE = re.compile(r"(src/continuum/[\w./]+\.py):(\d+)")

# kind -> regex finding the symbol's line(s) in the source. Named groups keep
# the patterns readable; ``decl`` also accepts an assignment because a symbol
# can grow from an alias into a class without the glossary noticing.
_PATTERNS = {
    "decl": r"^\s*(?:class|def|async\s+def)\s+{sym}\b|^\s*{sym}\s*[:=]",
    "assign": r"^\s*{sym}\s*[:=]",
    "ref": r"\b{sym}\b",
}

#: Every pointer the glossary is expected to carry. A new citation added to
#: docs/GLOSSARY.md without an entry here fails ``test_pointer_table_is_complete``
#: so it cannot ship unverified; a stale entry fails the same way.
EXPECTED: dict[tuple[str, int], tuple[str, str]] = {
    ("src/continuum/checkpoint/policy.py", 54): ("CheckpointTrigger", "decl"),
    ("src/continuum/checkpoint/policy.py", 64): ("RECOVERY", "assign"),
    ("src/continuum/checkpoint/manager.py", 184): ("checkpoint", "decl"),
    ("src/continuum/recovery/cleanup.py", 24): ("cleanup_ephemeral_artifacts", "decl"),
    ("src/continuum/concurrency/lease.py", 53): ("LeaseCoordinator", "decl"),
    ("src/continuum/recovery/ledger.py", 95): ("anchor", "assign"),
    ("src/continuum/recovery/ledger.py", 281): ("RecoveryLedger", "decl"),
    ("src/continuum/recovery/ledger.py", 288): ("LeaseCoordinator", "ref"),
    ("src/continuum/recovery/contract.py", 88): ("build_contract", "decl"),
    ("src/continuum/recovery/engine.py", 352): ("check_admissibility", "ref"),
    ("src/continuum/recovery/impact.py", 53): ("DependencyGraph", "decl"),
    ("src/continuum/recovery/planner.py", 112): ("RepairPlan", "decl"),
    ("src/continuum/reconcilers.py", 297): ("probe_authority_verdict", "decl"),
    ("src/continuum/security/provenance.py", 27): ("TrustLevel", "assign"),
    ("src/continuum/provenance_map.py", 60): ("CanonicalProvenance", "decl"),
    ("src/continuum/state/validator.py", 222): ("StateValidator", "decl"),
    ("src/continuum/adapters/actions.py", 31): ("AdapterAction", "decl"),
    ("src/continuum/adapters/actions.py", 60): ("run_action", "decl"),
    ("src/continuum/models.py", 103): ("StateStatus", "decl"),
    ("src/continuum/models.py", 198): ("Origin", "decl"),
    ("src/continuum/models.py", 233): ("EXTERNAL_MONITOR", "assign"),
    ("src/continuum/models.py", 1079): ("EnvResource", "decl"),
    ("src/continuum/models.py", 1091): ("EnvironmentSnapshot", "decl"),
    ("src/continuum/models.py", 1133): ("RecoveryContract", "decl"),
    ("src/continuum/models.py", 1207): ("StateCheckpoint", "decl"),
}


def _glossary_pointers() -> set[tuple[str, int]]:
    text = GLOSSARY.read_text(encoding="utf-8")
    return {(path, int(line)) for path, line in _POINTER_RE.findall(text)}


def _candidate_lines(source: list[str], symbol: str, kind: str) -> list[int]:
    pattern = _PATTERNS[kind].format(sym=re.escape(symbol))
    return [i + 1 for i, line in enumerate(source) if re.search(pattern, line)]


def test_pointer_table_is_complete() -> None:
    """Every glossary pointer is covered, and every expectation is still cited.

    Without the set comparison a citation could be added, edited, or deleted
    and only the person who happened to read that entry would know.
    """
    found = _glossary_pointers()
    assert found == set(EXPECTED), (
        f"glossary carries {len(found)} pointers, the table covers "
        f"{len(EXPECTED)}; untested: {sorted(found - set(EXPECTED))}, "
        f"stale: {sorted(set(EXPECTED) - found)}"
    )


@pytest.mark.parametrize(
    ("path", "line", "symbol", "kind"),
    [(key[0], key[1], value[0], value[1]) for key, value in EXPECTED.items()],
    ids=[f"{key[0]}:{key[1]}" for key in EXPECTED],
)
def test_pointer_names_its_symbol(path: str, line: int, symbol: str, kind: str) -> None:
    """The cited line lands within TOLERANCE lines of the symbol it names."""
    source = (ROOT / path).read_text(encoding="utf-8").splitlines()
    assert 0 < line <= len(source), f"{path}:{line} is out of range"

    candidates = _candidate_lines(source, symbol, kind)
    assert candidates, f"{symbol} is not declared as kind {kind!r} anywhere in {path}"

    nearest = min(abs(line - candidate) for candidate in candidates)
    assert nearest <= TOLERANCE, (
        f"{path}:{line} is {nearest} lines from the nearest {symbol} "
        f"({kind}) at line {min(candidates, key=lambda c: abs(line - c))}; "
        f"docs/GLOSSARY.md cites a line the symbol no longer occupies"
    )

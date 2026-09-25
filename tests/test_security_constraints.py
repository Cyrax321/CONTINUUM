"""Tests for the operator-authored constraint registry (issue #1412)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.models import Origin
from continuum.security.constraints import (
    DEFAULT_CONSTRAINTS_PATH,
    ConstraintLevel,
    ConstraintRegistry,
    ConstraintRegistryError,
    ConstraintSpec,
    constraints_digest,
    load_constraints,
    load_constraints_or_none,
)

#: Two hard constraints plus one soft, covering a scoped and a global entry.
GOOD = {
    "constraints": [
        {
            "id": "no-direct-db-writes",
            "level": "hard",
            "predicate": "The agent must not write to the primary database directly.",
            "scope": ["db.write", "db.admin"],
        },
        {
            "id": "no-prod-deploy",
            "level": "hard",
            "predicate": "Production deploys require human approval.",
            "scope": "deploy.",
        },
        {
            "id": "prefer-cache",
            "level": "soft",
            "predicate": "Prefer the cache over recomputing.",
        },
    ]
}


def _write(tmp_path: Path, data: object, name: str = "constraints.json") -> Path:
    target = tmp_path / ".continuum" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


def _spec(cid: str, **kw) -> ConstraintSpec:
    base = {"id": cid, "predicate": "p"}
    base.update(kw)
    return ConstraintSpec(**base)  # type: ignore[arg-type]


def test_load_valid_registry(tmp_path: Path) -> None:
    reg = load_constraints(_write(tmp_path, GOOD))
    assert isinstance(reg, ConstraintRegistry)
    assert len(reg) == 3
    assert set(reg.ids()) == {"no-direct-db-writes", "no-prod-deploy", "prefer-cache"}
    assert len(reg.hard()) == 2
    assert len(reg.soft()) == 1
    assert reg.get("prefer-cache") is not None
    assert reg.get("nope") is None


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    # A missing registry is an error, not an empty one: a deployment that meant
    # to ship constraints and did not should be loud about it.
    with pytest.raises(ConstraintRegistryError, match="not found"):
        load_constraints(tmp_path / "absent.json")


def test_load_or_none_relaxes_only_the_missing_case(tmp_path: Path) -> None:
    assert load_constraints_or_none(tmp_path / "absent.json") is None
    reg = load_constraints_or_none(_write(tmp_path, GOOD))
    assert reg is not None and len(reg) == 3


def test_default_path_is_the_shipped_convention() -> None:
    assert Path(".continuum/constraints.json") == DEFAULT_CONSTRAINTS_PATH


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        (Origin.HUMAN, True),
        (Origin.DETERMINISTIC, True),
        (Origin.EXTERNAL_AGENT, False),
        (Origin.LLM, False),
        (Origin.IMPORTED, False),
    ],
)
def test_operator_only_provenance(tmp_path: Path, origin: Origin, allowed: bool) -> None:
    # The no-self-certification rule: an agent cannot pin the constraints that
    # govern it. The check is at the boundary, so no call site can bypass it.
    path = _write(tmp_path, GOOD)
    if allowed:
        assert len(load_constraints(path, asserted_by=origin)) == 3
    else:
        with pytest.raises(ConstraintRegistryError, match="operator"):
            load_constraints(path, asserted_by=origin)


def test_digest_is_deterministic_and_order_independent() -> None:
    a = [_spec("b"), _spec("a")]
    b = list(reversed(a))
    assert constraints_digest(a) == constraints_digest(b)
    # The digest is over the canonical form, so a differing set differs.
    assert constraints_digest(a) != constraints_digest([_spec("a"), _spec("c")])
    # And the registry reports the same digest it computes.
    reg = ConstraintRegistry([_spec("a"), _spec("b")])
    assert reg.digest == constraints_digest([_spec("b"), _spec("a")])


def test_digest_is_stable_across_process_restarts() -> None:
    # A digest that drifted between runs could not be used to detect that the
    # governing set changed, so it must be a pure function of the content.
    from continuum.security.hashing import stable_hash

    canon = [
        {"id": "a", "level": "hard", "predicate": "p", "scope": []},
        {"id": "b", "level": "soft", "predicate": "p", "scope": ["x"]},
    ]
    assert constraints_digest(
        [_spec("a"), _spec("b", level=ConstraintLevel.SOFT, scope=("x",))]
    ) == stable_hash(canon)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({}, "constraints"),
        ({"constraints": "not-a-list"}, "constraints"),
        ({"constraints": []}, "no constraints"),
        ({"constraints": [{}]}, "id must be"),
        (
            {"constraints": [{"id": "ok", "predicate": "p"}, {"id": "ok", "predicate": "q"}]},
            "more than once",
        ),
        ({"constraints": [{"id": "bad id!", "predicate": "p"}]}, "id must be"),
        ({"constraints": [{"id": "x", "predicate": "p", "level": "maybe"}]}, "level must be"),
        ({"constraints": [{"id": "x", "predicate": "p", "scope": [123]}]}, "scope entry"),
        ({"constraints": [{"id": "x"}]}, "predicate"),
    ],
)
def test_malformed_registries_fail_closed(tmp_path: Path, data: object, fragment: str) -> None:
    with pytest.raises(ConstraintRegistryError, match=fragment):
        load_constraints(_write(tmp_path, data))


def test_corrupt_json_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / ".continuum" / "constraints.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ this is not json ", encoding="utf-8")
    with pytest.raises(ConstraintRegistryError, match="not valid JSON"):
        load_constraints(target)


def test_constraint_count_cap(tmp_path: Path) -> None:
    data = {"constraints": [{"id": f"c{i}", "predicate": "p"} for i in range(300)]}
    with pytest.raises(ConstraintRegistryError, match="cap"):
        load_constraints(_write(tmp_path, data))


def test_scope_matches_by_prefix_and_star() -> None:
    scoped = _spec("db", scope=("db.",))
    assert scoped.matches_scope("db.write")
    assert scoped.matches_scope("db")
    assert not scoped.matches_scope("deploy.prod")

    star = _spec("all", scope=("*",))
    assert star.matches_scope("anything.at.all")

    bare = _spec("bare")
    # An unspecfied scope governs everything: a global constraint.
    assert bare.matches_scope("db.write")
    assert bare.matches_scope("deploy")


def test_in_scope_filters_by_level() -> None:
    reg = ConstraintRegistry(
        [
            _spec("hard-db", level=ConstraintLevel.HARD, scope=("db.",)),
            _spec("soft-cache", level=ConstraintLevel.SOFT),
        ]
    )
    hits = reg.in_scope("db.write")
    assert {c.id for c in hits} == {"hard-db", "soft-cache"}
    assert {c.id for c in reg.in_scope("db.write", level=ConstraintLevel.HARD)} == {"hard-db"}


def test_predicate_is_never_evaluated_as_code() -> None:
    # Predicates are operator text, digested and stored, never executed. A
    # predicate that would be dangerous if evaluated must load harmlessly.
    evil = "os.system('rm -rf /')"
    spec = _spec("x", predicate=evil)
    assert spec.predicate == evil
    assert constraints_digest([spec])  # digesting, not executing

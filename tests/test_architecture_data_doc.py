"""Guard the counts in references/architecture-data.md against the live code (#1108).

That doc is meant to be a source-verified map of the system, but every count in
it drifted silently: CLI commands were documented as 14 while the parser
registered 47, action states as 7 while the enum had 8, and the MCP tool table
listed 11 of 13 tools with a read-only/mutating split that contradicted the
diagram four sections later. Line numbers cannot be guarded (they move with
every refactor), but counts can. Each claim below is re-measured from the code
the doc points at, so a stale number fails CI instead of misleading a reader.

Adding a CLI command, MCP tool, event type, action state, checkpoint policy,
recovery mode, or ``SemanticState`` field means updating
references/architecture-data.md in the same change.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "references" / "architecture-data.md"
SERVER = ROOT / "src" / "continuum" / "mcp" / "server.py"
POLICY = ROOT / "src" / "continuum" / "checkpoint" / "policy.py"

# Every state carries bookkeeping that is not part of the semantic picture the
# doc enumerates: identity, versioning, provenance timestamps, and the
# ``unprojectable_*`` fields a degraded fold uses to name where it stopped. A
# new field here is metadata, not a new semantic field.
_NON_SEMANTIC_STATE_FIELDS = frozenset(
    {
        "run_id",
        "version",
        "source_sequence",
        "created_at",
        "updated_at",
        "status",
        "unprojectable_at_sequence",
        "unprojectable_event_type",
        "unprojectable_reason",
    }
)


def _doc() -> str:
    assert DOC.exists(), f"{DOC} moved: update the path in {__name__}"
    return DOC.read_text(encoding="utf-8")


def _stated(pattern: str, label: str, group: int = 1) -> int:
    """The figure the doc states for ``label``, failing if it states none.

    Absence is an error, not a skip: a claim the regex can no longer find has
    lost its spine, and silently passing it is exactly how this doc drifted.
    """
    match = re.search(pattern, _doc(), re.MULTILINE)
    assert match, (
        f"{DOC} no longer states {label} where {__name__} expects it (pattern {pattern!r})"
    )
    return int(match.group(group))


def _cli_command_count() -> int:
    from continuum.cli.main import build_parser

    parser = build_parser()
    subactions = [
        action
        for action in parser._subparsers._group_actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    assert len(subactions) == 1, f"expected one subparser group, found {len(subactions)}"
    return len(subactions[0].choices)


def _mcp_tool_split() -> tuple[int, int]:
    """(read-only, mutating) tool counts, read off the ``@server.tool`` annotations."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    read_only = mutating = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        ):
            continue
        names = {
            keyword.value.id
            for keyword in _annotation_keywords(node)
            if isinstance(keyword.value, ast.Name)
        }
        assert names, f"{node.name} is registered as a tool without an annotation"
        assert len(names) == 1, f"{node.name} declares contradictory annotations: {names}"
        if names.pop() == "read_only":
            read_only += 1
        else:
            mutating += 1
    return read_only, mutating


def _annotation_keywords(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.keyword]:
    keywords: list[ast.keyword] = []
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            keywords.extend(decorator.keywords)
    return keywords


def _checkpoint_policy_count() -> int:
    """Concrete ``CheckpointPolicy`` subclasses declared in ``policy.py``."""
    tree = ast.parse(POLICY.read_text(encoding="utf-8"))
    return sum(
        1
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "CheckpointPolicy" for base in node.bases)
    )


def test_cli_command_count() -> None:
    assert _stated(r"## 15\. CLI commands \((\d+)\)", "the CLI command count") == (
        _cli_command_count()
    )


def test_mcp_tool_counts() -> None:
    read_only, mutating = _mcp_tool_split()
    total = read_only + mutating
    assert _stated(r"(\d+) tools, all names prefixed,", "the MCP tool total") == total
    stated_read_only = _stated(
        r"Read-only count = (\d+), mutating count = \d+\.", "the read-only tool count"
    )
    stated_mutating = _stated(
        r"Read-only count = (\d+), mutating count = (\d+)\.", "the mutating tool count", group=2
    )
    assert (stated_read_only, stated_mutating) == (read_only, mutating)


def test_event_type_count() -> None:
    from continuum.events import EventType

    assert _stated(r"(\d+) event types \(", "the event type count") == len(list(EventType))


def test_action_status_count() -> None:
    from continuum.models import ActionStatus

    assert _stated(r"\((\d+) values\)", "the action state count") == len(list(ActionStatus))


def test_checkpoint_policy_count() -> None:
    assert _stated(r"(\d+) policies \(`policy.py`\):", "the checkpoint policy count") == (
        _checkpoint_policy_count()
    )


def test_recovery_mode_count() -> None:
    from continuum.models import RecoveryMode

    assert _stated(r"^(\d+) modes\. Most cautious wins", "the recovery mode count") == len(
        list(RecoveryMode)
    )


def test_semantic_state_field_count() -> None:
    from continuum.models import SemanticState

    live = len(set(SemanticState.model_fields) - _NON_SEMANTIC_STATE_FIELDS)
    assert _stated(r"^(\d+) semantic fields \(exact attribute names\)", "the field count") == live

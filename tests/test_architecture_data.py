"""Guard the countable claims in references/architecture-data.md (#1108).

That file is the source-verified companion to references/architecture.md: every
row is a fact read from the code, and it is the reference a contributor opens to
locate the code behind a concept. It drifted hard (#1108) until it documented 14
of 46 CLI commands and 29 of 51 event types, listed 7 of 8 action states, and
cited ``file:line`` pointers that landed on blank lines and unrelated code. The
file's own header still promises "no value here is inferred", so this module
re-derives the claims from the live tree and fails the suite when doc and code
disagree.

Counts and enum membership come from introspection, never from a hardcoded
expectation, so a value added to ``EventType`` breaks this test even if nobody
touched the doc. Symbol existence is checked because that is what rots silently:
#1108's ``_ORDER`` citation survived a rename to ``SEVERITY``, a move to another
module, and a change of what it ranks, and nothing noticed. Line numbers are
deliberately not asserted, they move on every refactor and the doc cites them
for a reader, not for a test.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "references" / "architecture-data.md"


def _doc() -> str:
    assert DOC.exists(), f"{DOC} moved; update the guard path"
    return DOC.read_text(encoding="utf-8")


def _documented_count(heading_fragment: str) -> int:
    """The ``(N)`` off a heading like ``## 15. CLI commands (46)``."""
    match = re.search(rf"^##+ .*{re.escape(heading_fragment)}.*?\((\d+)\)", _doc(), re.MULTILINE)
    assert match, f"{DOC} has no heading naming {heading_fragment!r} with an (N) count"
    return int(match.group(1))


def _documented_event_type_count() -> int:
    """``- 51 event types`` in section 7's opening bullet."""
    match = re.search(r"^-\s+(\d+)\s+event types", _doc(), re.MULTILINE)
    assert match, f"{DOC} no longer opens section 7 with '- N event types'"
    return int(match.group(1))


def _documented_action_state_count() -> int:
    """``(8 values)`` closing the action states paragraph in section 8."""
    match = re.search(r"Action states.*?\((\d+)\s+values?\)", _doc(), re.DOTALL)
    assert match, f"{DOC} no longer closes the action states list with '(N values)'"
    return int(match.group(1))


def _fenced_block_after(marker: str) -> str:
    """The fenced code body that follows ``marker`` in the doc."""
    text = _doc()
    start = text.find(marker)
    assert start != -1, f"{DOC} no longer contains {marker!r}"
    block_start = text.find("```", start)
    block_end = text.find("```", block_start + 3)
    assert block_start != -1 and block_end != -1, f"{DOC} lost the fenced block after {marker!r}"
    # The opening fence may carry a language tag (```text); it is not body.
    return text[block_start + 3 : block_end].split("\n", 1)[-1]


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    subcommand_actions = [
        action for action in parser._actions if isinstance(getattr(action, "choices", None), dict)
    ]
    assert subcommand_actions, "build_parser no longer has a single subparsers action"
    return set(subcommand_actions[0].choices)


def test_cli_command_count_matches_parser() -> None:
    """Section 15's count comes from ``build_parser`` subcommands."""
    from continuum.cli.main import build_parser

    live = _subcommands(build_parser())
    assert _documented_count("CLI commands") == len(live), (
        "references/architecture-data.md documents a stale CLI command count; "
        "recount from build_parser() subcommands"
    )


def test_cli_command_list_matches_parser() -> None:
    """Section 15 names every subcommand the parser accepts."""
    from continuum.cli.main import build_parser

    live = _subcommands(build_parser())
    # Section 15 lists the verbs as a comma-separated paragraph, not a fence.
    section = _doc().split("## 15. CLI commands")[1]
    documented = set(re.findall(r"`([\w-]+)`", section))
    missing = live - documented
    assert not missing, (
        f"references/architecture-data.md omits CLI commands {sorted(missing)}; "
        "the enumeration in section 15 is incomplete"
    )


def test_event_type_count_matches_enum() -> None:
    """Section 7's count is whatever ``EventType`` has today."""
    from continuum.events import EventType

    assert _documented_event_type_count() == len(EventType), (
        "references/architecture-data.md documents a stale event type count; "
        "recount from len(EventType)"
    )


def test_event_type_list_matches_enum() -> None:
    """Section 7's complete list names every member and invents none."""
    from continuum.events import EventType

    live = {member.name for member in EventType}
    documented = {
        name.strip() for name in _fenced_block_after("Complete list:").replace("\n", " ").split(",")
    }
    missing = live - documented
    assert not missing, (
        f"references/architecture-data.md omits event types {sorted(missing)}; "
        "the complete list in section 7 is incomplete"
    )
    invented = documented - live
    assert not invented, (
        f"references/architecture-data.md names event types {sorted(invented)} "
        "that EventType no longer defines"
    )


def test_action_status_matches_enum() -> None:
    """Section 8's count and names come from ``ActionStatus``."""
    from continuum.models import ActionStatus

    live = {member.name for member in ActionStatus}
    assert _documented_action_state_count() == len(live), (
        "references/architecture-data.md documents a stale action state count; "
        "recount from len(ActionStatus)"
    )
    text = _doc()
    for name in live:
        assert name in text, (
            f"references/architecture-data.md does not name action state {name}; "
            "section 8 lists the enum values"
        )


def test_recovery_modes_and_safety_match_enums() -> None:
    """Section 11's seven modes and their safety classes are the real enums."""
    from continuum.models import RecoveryMode, RecoverySafety

    text = _doc()
    for mode in RecoveryMode:
        assert f"`{mode.name}`" in text, f"section 11 does not list mode {mode.name}"
    for safety in RecoverySafety:
        assert f"`{safety.name}`" in text, f"section 11 does not name safety {safety.name}"


@pytest.mark.parametrize(
    ("module", "symbol"),
    [
        ("checkpoint/policy.py", "ManualPolicy"),
        ("checkpoint/policy.py", "IntervalPolicy"),
        ("checkpoint/policy.py", "EventPolicy"),
        ("checkpoint/policy.py", "SemanticPolicy"),
        ("checkpoint/policy.py", "ContextPressurePolicy"),
        ("checkpoint/policy.py", "HybridPolicy"),
        ("checkpoint/policy.py", "default_policy"),
        ("actions/reconciliation.py", "ProbeReconciler"),
        ("actions/reconciliation.py", "ManualReconciler"),
        ("actions/reconciliation.py", "AssumeNotOccurredReconciler"),
        ("state/extractor.py", "DeterministicExtractor"),
        ("state/extractor.py", "LLMExtractor"),
        ("adapters/generic.py", "GenericAgentAdapter"),
        ("adapters/openai.py", "OpenAIAgentAdapter"),
        ("adapters/langgraph.py", "LangGraphAgentAdapter"),
        ("storage/sqlite.py", "SQLiteStorage"),
        ("storage/base.py", "CorruptedRecord"),
        ("storage/base.py", "ConcurrentWriteError"),
        ("recovery/engine.py", "SEVERITY"),
        ("recovery/engine.py", "_SAFETY_FOR_MODE"),
        ("models.py", "SemanticState"),
        ("models.py", "Origin"),
        ("models.py", "ActionStatus"),
        ("events.py", "EventType"),
        ("events.py", "EventLog"),
        ("recovery/engine.py", "RecoveryEngine"),
    ],
)
def test_documented_symbol_exists(module: str, symbol: str) -> None:
    """Every symbol the doc names still exists where it is cited.

    A rename or deletion while the citation survives is the failure that
    #1108's ``_ORDER`` -> ``SEVERITY`` drift made invisible: the pointer kept
    pointing, at a symbol that was no longer there and had never meant what
    the prose said.
    """
    source = (ROOT / "src" / "continuum" / module).read_text(encoding="utf-8")
    pattern = rf"^(?:class|def)\s+{re.escape(symbol)}\b|^{re.escape(symbol)}\s*[:=]"
    assert re.search(pattern, source, re.MULTILINE), (
        f"{symbol} is cited in references/architecture-data.md but is not "
        f"declared in src/continuum/{module}; the citation is now dangling"
    )

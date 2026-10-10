"""The instruction filenames each coding IDE actually reads.

Every IDE without a hook surface still reads instructions from a file, and
none of them read the same one. Eight copies of the same guidance is eight
chances to be wrong, so the content lives in exactly one place
(:mod:`continuum.agents.instructions`) and this module is the table saying
where each copy goes.

Everything that differs between targets is data, not code, matching the shape
:data:`continuum.mcp.install.HOST_PROFILES` and
:data:`continuum.clienthooks.CLIENT_PROFILES` already use. Adding a ninth IDE
is one entry here.

``cursor`` and ``cursor-legacy`` are the same IDE at two generations:
``.cursor/rules/*.mdc`` is current and ``.cursorrules`` is the single-file
form Cursor still reads for older projects. They are separate targets
because a project may legitimately want one and not the other, and because
their file formats differ.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["InstructionTarget", "TARGETS", "TARGET_IDS", "target_for"]


@dataclass(frozen=True)
class InstructionTarget:
    """One instruction file an IDE reads, and how to render it.

    ``frontmatter`` is a tuple of pairs rather than a dict so the whole
    dataclass stays hashable and genuinely immutable: a registry that a
    caller can mutate is a registry that drifts.
    """

    id: str
    path: str
    summary: str
    frontmatter: tuple[tuple[str, str], ...] = ()


#: One entry per instruction filename, keyed by the ``--target`` name.
TARGETS: dict[str, InstructionTarget] = {
    target.id: target
    for target in (
        InstructionTarget(
            id="agents",
            path="AGENTS.md",
            summary="the cross-vendor agent instructions (Codex, opencode, Cursor)",
        ),
        InstructionTarget(
            id="claude",
            path="CLAUDE.md",
            summary="Claude Code's project instructions",
        ),
        InstructionTarget(
            id="gemini",
            path="GEMINI.md",
            summary="Gemini CLI's context file",
        ),
        InstructionTarget(
            id="copilot",
            path=".github/copilot-instructions.md",
            summary="GitHub Copilot's repository-wide instructions",
        ),
        InstructionTarget(
            id="junie",
            path=".junie/guidelines.md",
            summary="JetBrains Junie's project guidelines",
        ),
        InstructionTarget(
            id="windsurf",
            path=".windsurfrules",
            summary="Windsurf's rules file",
        ),
        InstructionTarget(
            id="cursor",
            path=".cursor/rules/continuum.mdc",
            summary="Cursor's current rules format",
            frontmatter=(("description", "CONTINUUM project instructions"), ("alwaysApply", "true")),
        ),
        InstructionTarget(
            id="cursor-legacy",
            path=".cursorrules",
            summary="Cursor's legacy single-file rules",
        ),
    )
}

#: The ``--target`` choices, in the order they are listed to a reader.
TARGET_IDS: tuple[str, ...] = tuple(TARGETS)


def target_for(identifier: str) -> InstructionTarget:
    """Return the target named ``identifier``.

    Raises ``KeyError`` rather than returning ``None``: the CLI validates the
    choice against :data:`TARGET_IDS` before calling, so reaching the failure
    means the registry and the parser disagree, which is a bug here rather
    than an operator mistake.
    """

    return TARGETS[identifier]

"""The instruction generator must never destroy a human's file.

Four properties are pinned here, because each one is a way the feature could
quietly become destructive: round-tripping (a regenerate is byte-identical, so
regenerating is safe to do at any time), drift detection (a committed copy
that no longer matches its source fails a check rather than sitting there),
refusal (a hand-written file is never overwritten), and narrow removal (only
fingerprint-carrying files are deleted).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.agents import (
    GENERATED_MARKER,
    TARGET_IDS,
    TARGETS,
    check,
    fingerprint_of,
    install,
    remove,
    render,
    source_digest,
)

SOURCE = "Source of truth.\n\n- one rule\n- another rule\n"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "instructions.md"
    path.write_text(SOURCE, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def test_every_target_round_trips_byte_identically(root: Path, source: Path) -> None:
    """Generate, read back, regenerate: the second write must be a no-op.

    This is the property that makes it safe to regenerate at all. A rendered
    file carrying a timestamp, or trailing whitespace normalised on read but
    not on write, would produce a diff every time and the banner would become
    noise people learn to ignore.
    """

    for target_id in TARGET_IDS:
        first = install(target_id, root=root, source=source)
        original = first.path.read_bytes()
        second = install(target_id, root=root, source=source)
        assert second.path.read_bytes() == original, f"{target_id} is not byte-stable"
        assert second.status == "present", f"{target_id} rewrote an unchanged file"


def test_round_trip_holds_with_crlf_and_trailing_blanks(source: Path, tmp_path: Path) -> None:
    """Normalisation keeps a Windows checkout from looking permanently drifted."""

    crlf = tmp_path / "crlf.md"
    crlf.write_text(SOURCE.replace("\n", "\r\n") + "\n\n\n", encoding="utf-8")
    assert source_digest(crlf.read_text(encoding="utf-8")) == source_digest(SOURCE)


def test_every_target_carries_the_marker_command_and_digest(root: Path, source: Path) -> None:
    """The banner has to name the command, or a reader cannot fix the file."""

    for target_id in TARGET_IDS:
        result = install(target_id, root=root, source=source)
        text = result.path.read_text(encoding="utf-8")
        assert GENERATED_MARKER in text, f"{target_id} has no marker"
        assert f"continuum agents install --target {target_id}" in text
        marks = fingerprint_of(text)
        assert marks is not None, f"{target_id} has no readable fingerprint"
        assert marks.source_sha256 == source_digest(SOURCE)
        assert marks.command == f"continuum agents install --target {target_id}"


def test_cursor_target_leads_with_its_frontmatter(root: Path, source: Path) -> None:
    """``.mdc`` rules are only read when the YAML fence comes first."""

    result = install("cursor", root=root, source=source)
    assert result.path.relative_to(root).as_posix() == ".cursor/rules/continuum.mdc"
    assert result.path.read_text(encoding="utf-8").startswith("---\n")


def test_drift_is_detected_and_named(root: Path, source: Path) -> None:
    """A committed copy that no longer matches its source fails the check."""

    install("claude", root=root, source=source)
    assert check("claude", root=root, source=source).state == "current"

    source.write_text(SOURCE + "\n- a newly added rule\n", encoding="utf-8")
    drifted = check("claude", root=root, source=source)
    assert drifted.state == "drifted"
    assert "source changed" in drifted.detail
    assert "continuum agents install --target claude" in drifted.detail


def test_hand_edited_body_is_reported_as_drift_not_silence(root: Path, source: Path) -> None:
    """Editing the body keeps the digest, so the check must still catch it."""

    result = install("gemini", root=root, source=source)
    result.path.write_text(
        result.path.read_text(encoding="utf-8") + "\nsomething nobody sanctioned\n",
        encoding="utf-8",
    )
    drifted = check("gemini", root=root, source=source)
    assert drifted.state == "drifted"
    assert "edited by hand" in drifted.detail


def test_existing_non_generated_file_is_refused_not_overwritten(root: Path) -> None:
    """The core safety contract, same as the hook and MCP installers."""

    handwritten = root / "AGENTS.md"
    handwritten.write_text("# our own rules\n\nDo not clobber me.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        install("agents", root=root)

    assert handwritten.read_text(encoding="utf-8") == "# our own rules\n\nDo not clobber me.\n"


def test_refusal_fires_for_an_empty_file_too(root: Path) -> None:
    """An empty file is still a file someone made; adoption is not implied."""

    empty = root / ".cursorrules"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        install("cursor-legacy", root=root)
    assert empty.read_text(encoding="utf-8") == ""


def test_file_mentioning_the_generator_without_a_digest_is_still_refused(root: Path) -> None:
    """The marker alone must not be enough to claim ownership of a file."""

    impostor = root / "CLAUDE.md"
    impostor.write_text(f"# notes\n\n{GENERATED_MARKER} was mentioned here.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        install("claude", root=root)
    assert GENERATED_MARKER in impostor.read_text(encoding="utf-8")


def test_unmanaged_existing_file_is_reported_separately_from_drift(root: Path) -> None:
    """A conflict and a drift are different problems and get different words."""

    (root / "CLAUDE.md").write_text("# ours\n", encoding="utf-8")
    result = check("claude", root=root)
    assert result.state == "unmanaged"
    assert "not written by this generator" in result.detail


def test_absent_target_is_reported_as_absent(root: Path) -> None:
    """Not having a target is not drift; it is a target nobody generated."""

    assert check("windsurf", root=root).state == "absent"


def test_remove_deletes_only_generated_files(root: Path) -> None:
    """remove must not become a delete command aimed at someone's own file."""

    generated = install("windsurf", root=root).path
    handwritten = root / "AGENTS.md"
    handwritten.write_text("# our own rules\n", encoding="utf-8")

    assert remove("windsurf", root=root) is True
    assert not generated.exists()
    assert handwritten.exists(), "remove deleted a file the generator did not write"

    assert remove("agents", root=root) is False
    assert handwritten.read_text(encoding="utf-8") == "# our own rules\n"


def test_remove_is_a_no_op_on_a_missing_target(root: Path) -> None:
    assert remove("junie", root=root) is False


def test_install_reppoints_an_existing_generated_file(root: Path, source: Path) -> None:
    """Re-running after a source edit updates, rather than duplicating or failing."""

    install("copilot", root=root, source=source)
    source.write_text(SOURCE + "- updated\n", encoding="utf-8")
    again = install("copilot", root=root, source=source)
    assert again.status == "updated"
    assert check("copilot", root=root, source=source).state == "current"


def test_missing_source_raises_rather_than_rendering_nothing(root: Path, tmp_path: Path) -> None:
    """An empty instruction file is the one failure a user cannot notice."""

    with pytest.raises(FileNotFoundError):
        install("agents", root=root, source=tmp_path / "nope.md")
    assert not (root / "AGENTS.md").exists()


def test_registry_covers_every_documented_instruction_filename() -> None:
    """The eight filenames are the feature; a silent drop is a regression."""

    paths = {target.path for target in TARGETS.values()}
    assert paths == {
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        ".github/copilot-instructions.md",
        ".junie/guidelines.md",
        ".windsurfrules",
        ".cursor/rules/continuum.mdc",
        ".cursorrules",
    }


def test_default_source_ships_and_names_the_regenerate_command() -> None:
    """The shipped source is the one a fresh checkout renders from."""

    from continuum.agents import DEFAULT_SOURCE

    assert DEFAULT_SOURCE.is_file()
    text = DEFAULT_SOURCE.read_text(encoding="utf-8")
    assert "continuum agents install" in text
    assert fingerprint_of(render(TARGETS["agents"], text, command="x")) is not None


def test_check_agrees_with_render_for_every_target(root: Path, source: Path) -> None:
    """check must never disagree with what install would write."""

    for target_id in TARGET_IDS:
        install(target_id, root=root, source=source)
        assert check(target_id, root=root, source=source).state == "current"

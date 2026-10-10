"""Tests for the config format layer (issue #1597).

``continuum mcp install`` writes into editors' own configuration files. The
failure this module exists to prevent is not a crash: it is a *quiet* rewrite
of a file this tool does not understand, where the user gets back something
that parses but is no longer what they wrote. So the tests below are mostly
refusal tests. Each one pins a file that must be reported and left alone.

The TOML and YAML tests skip when the optional parser is absent, because the
format layer deliberately does not depend on one: it refuses with an install
hint instead. A skip here means the parser is missing on this machine, not
that the format is unsupported.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from continuum.mcp import configfmt

#: A registration shaped the way the JSON hosts spell it. Every format has to
#: carry the same structure, because what a host reads is the structure and
#: not the bytes.
REGISTRATION = {
    "mcpServers": {
        "continuum-mcp": {
            "command": "/opt/venv/bin/continuum-mcp",
            "args": ["--db", "/home/op/proj/continuum.db"],
            "env": {"CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code"},
        }
    }
}

needs_toml = pytest.mark.skipif(
    importlib.util.find_spec("tomli_w") is None,
    reason="writing TOML needs tomli-w; the format layer refuses without it by design",
)
needs_yaml = pytest.mark.skipif(
    importlib.util.find_spec("yaml") is None,
    reason="YAML support needs PyYAML; the format layer refuses without it by design",
)


def round_trips(fmt: str, data: dict[str, Any]) -> bool:
    """``data`` survives a write and a read in ``fmt`` unchanged."""
    rendered = configfmt.render_document(fmt, data)
    return configfmt._parse(fmt, rendered, Path("round-trip")) == data


# --------------------------------------------------------------------------- #
# round trip
# --------------------------------------------------------------------------- #


def test_json_round_trips_the_registration() -> None:
    """What a JSON host reads back is what was written, exactly."""
    assert round_trips("json", REGISTRATION)


@needs_yaml
def test_yaml_round_trips_the_registration() -> None:
    """Same promise for YAML, which has no JSON to lean on."""
    assert round_trips("yaml", REGISTRATION)


@needs_toml
def test_toml_round_trips_the_registration() -> None:
    """Same promise for TOML, the one format with no standard-library writer."""
    assert round_trips("toml", REGISTRATION)


@pytest.mark.parametrize("fmt", ["json", "toml", "yaml"])
def test_every_format_is_registered(fmt: str) -> None:
    """A profile naming a format this module does not know is a bug, not a crash.

    ``FORMATS`` is what a profile is validated against, so an unknown format
    has to be visible rather than falling through to a default.
    """
    assert fmt in configfmt.FORMATS


def test_an_unknown_format_is_refused_by_name() -> None:
    """A typo in a profile names itself instead of raising something opaque."""
    with pytest.raises(configfmt.ConfigError, match="unknown config format 'yml'"):
        configfmt.render_document("yml", {})


# --------------------------------------------------------------------------- #
# refusal: the file itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fmt", "broken", "expected"),
    [
        ("json", "{not json", "is not valid JSON"),
        ("json", "[]\n", "does not contain a JSON object"),
        ("json", '"just a string"\n', "does not contain a JSON object"),
    ],
)
def test_a_json_file_the_command_cannot_read_is_never_rewritten(
    tmp_path: Path, fmt: str, broken: str, expected: str
) -> None:
    """The refusal contract from ``_load_object``, preserved verbatim per format.

    A settings file holds unrelated configuration, so every shape that is not
    the one expected is reported rather than replaced. The wording is pinned
    because it is what the CLI prints to the operator.
    """
    settings = tmp_path / "settings.json"
    settings.write_text(broken, encoding="utf-8")

    with pytest.raises(configfmt.ConfigError, match=expected):
        configfmt.read_document(fmt, settings)

    assert settings.read_text(encoding="utf-8") == broken


@needs_yaml
def test_a_yaml_file_that_will_not_parse_is_never_rewritten(tmp_path: Path) -> None:
    """Unparseable YAML is reported, not recreated.

    ``tab`` is the cheapest unparseable YAML there is: indentation may not
    contain a tab, so it fails in every YAML parser.
    """
    settings = tmp_path / "config.yaml"
    settings.write_text("mcpServers:\n\t- name: x\n", encoding="utf-8")

    with pytest.raises(configfmt.ConfigError, match="is not valid YAML"):
        configfmt.read_document("yaml", settings)

    assert settings.read_text(encoding="utf-8") == "mcpServers:\n\t- name: x\n"


@needs_yaml
def test_a_yaml_document_that_is_a_list_is_never_rewritten(tmp_path: Path) -> None:
    """A YAML file whose root is a list is refused the same way JSON's is."""
    settings = tmp_path / "config.yaml"
    settings.write_text("- one\n- two\n", encoding="utf-8")

    with pytest.raises(configfmt.ConfigError, match="does not contain a YAML mapping"):
        configfmt.read_document("yaml", settings)

    assert settings.read_text(encoding="utf-8") == "- one\n- two\n"


@needs_toml
def test_a_toml_file_that_will_not_parse_is_never_rewritten(tmp_path: Path) -> None:
    """Unparseable TOML is reported, not recreated."""
    settings = tmp_path / "config.toml"
    settings.write_text("[mcp_servers\n", encoding="utf-8")

    with pytest.raises(configfmt.ConfigError, match="is not valid TOML"):
        configfmt.read_document("toml", settings)

    assert settings.read_text(encoding="utf-8") == "[mcp_servers\n"


@needs_toml
def test_a_toml_document_that_is_not_a_table_is_refused(tmp_path: Path) -> None:
    """A TOML array at the root has no place to put the registration."""
    settings = tmp_path / "config.toml"
    settings.write_text("key = [1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(configfmt.ConfigError, match="does not contain a TOML table"):
        configfmt.read_document("toml", settings)

    assert settings.read_text(encoding="utf-8") == "key = [1, 2, 3]\n"


def test_an_absent_file_reads_as_an_empty_mapping(tmp_path: Path) -> None:
    """A config the editor has not written yet is not an error.

    This is what lets install create a registration in a fresh profile, and it
    is the one "empty" case that is folded rather than refused.
    """
    assert configfmt.read_document("json", tmp_path / "absent.json") == {}


@needs_yaml
def test_a_blank_yaml_file_reads_as_an_empty_mapping(tmp_path: Path) -> None:
    """A zero-byte YAML file parses to null; that is an editor, not a mistake."""
    settings = tmp_path / "config.yaml"
    settings.write_text("\n\n", encoding="utf-8")

    assert configfmt.read_document("yaml", settings) == {}


# --------------------------------------------------------------------------- #
# refusal: the value
# --------------------------------------------------------------------------- #


def test_toml_has_no_null_and_says_where_one_is(tmp_path: Path) -> None:
    """A null in a TOML document is refused by path, not dropped.

    Dropping the key would change what the host reads without saying so, and
    inventing a placeholder would change it differently. Both are worse than
    an error that names the key.
    """
    document = {"mcp_servers": {"continuum-mcp": {"command": "x", "cwd": None}}}

    with pytest.raises(configfmt.ConfigError) as excinfo:
        configfmt.write_document("toml", tmp_path / "config.toml", document)

    assert "mcp_servers.continuum-mcp.cwd" in str(excinfo.value)
    assert "TOML has no null" in str(excinfo.value)
    assert not (tmp_path / "config.toml").exists(), "a refused write must leave no file"


# --------------------------------------------------------------------------- #
# refusal: what a rewrite would destroy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["toml", "yaml"])
@needs_yaml
def test_a_commented_file_is_refused_rather_than_flattened(tmp_path: Path, fmt: str) -> None:
    """Comments are the user's; a re-serialisation would delete them silently.

    Comments do not survive parsing at all in either format, so there is no
    way to write one back through a document model. That makes an existing
    commented file the one case where a whole-file write is destructive, and
    it is reported instead.
    """
    settings = tmp_path / f"config.{fmt}"
    marker = "# a note the operator wrote\n"
    settings.write_text(marker, encoding="utf-8")

    with pytest.raises(configfmt.ConfigError) as excinfo:
        configfmt.write_document(fmt, settings, REGISTRATION)

    assert "comments" in str(excinfo.value)
    assert settings.read_text(encoding="utf-8") == marker


def test_json_does_not_refuse_on_comments_json_has_none(tmp_path: Path) -> None:
    """JSON has no comment syntax, so the JSON path never trips that guard.

    The guard is the reason every JSON host can still be rewritten wholesale,
    and a JSON string that merely *contains* a hash is not a comment.
    """
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"note": "colour #ff0000"}), encoding="utf-8")

    configfmt.write_document("json", settings, REGISTRATION)

    assert json.loads(settings.read_text(encoding="utf-8")) == REGISTRATION


def test_a_hash_inside_a_string_is_not_treated_as_a_comment(tmp_path: Path) -> None:
    """The comment guard looks at the first character of a line, nothing else.

    A smarter scan would try to tell a quoted ``#`` from a comment marker and
    would misfire on the very files it exists to protect. A line whose first
    non-blank character is ``#`` is the only thing counted.
    """
    assert not configfmt._has_comment_line('key: "value # not a comment"\n')
    assert configfmt._has_comment_line("  # indented comment\n")


# --------------------------------------------------------------------------- #
# refusal: an absent optional parser
# --------------------------------------------------------------------------- #


def test_a_missing_yaml_parser_is_reported_with_the_command_that_fixes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PyYAML means a named remedy, not a traceback.

    ``mcp install`` already refuses for a missing MCP SDK the same way, so the
    two failures read identically to an operator: here is what is missing,
    here is the command.
    """
    settings = tmp_path / "config.yaml"
    settings.write_text("mcpServers: []\n", encoding="utf-8")
    real_import_module = configfmt.importlib.import_module

    def without_yaml(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(configfmt.importlib, "import_module", without_yaml)

    with pytest.raises(configfmt.ConfigError) as excinfo:
        configfmt.read_document("yaml", settings)

    message = str(excinfo.value)
    assert "PyYAML" in message
    assert 'pip install "continuum-agent[mcp]"' in message


def test_a_missing_toml_writer_is_reported_with_the_command_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same contract for the TOML writer, which the standard library lacks."""
    real_import_module = configfmt.importlib.import_module

    def without_tomli_w(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "tomli_w":
            raise ImportError("No module named 'tomli_w'")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(configfmt.importlib, "import_module", without_tomli_w)

    with pytest.raises(configfmt.ConfigError) as excinfo:
        configfmt.render_document("toml", REGISTRATION)

    message = str(excinfo.value)
    assert "tomli-w" in message
    assert 'pip install "continuum-agent[mcp]"' in message


def test_reading_toml_never_needs_a_writer() -> None:
    """``tomllib`` is standard library, so reading a TOML config always works.

    Only writing needs the extra. An operator who wants to inspect their file,
    or a ``mcp remove`` on a host this tool cannot write, must not be blocked
    by the missing writer.
    """
    import tomllib

    parsed = tomllib.loads('[mcp_servers.x]\ncommand = "c"\n')
    assert parsed == {"mcp_servers": {"x": {"command": "c"}}}

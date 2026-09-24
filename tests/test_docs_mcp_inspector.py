"""Guard the documented MCP inspector command against a config it cannot open.

``references/testing.md`` walks the MCP protocol boundary by pointing
``@modelcontextprotocol/inspector --cli`` at a server config. The config has to
be a file the repository actually ships and the ``--server`` name has to be an
entry inside it, or the copy-pasted command dies at the exact step that is
meant to exercise the boundary (issue #1395: the walkthrough named
``mcp-config.json``, which has never existed in the tree, while the tracked
config is ``.mcp.json``).

Every markdown walkthrough under ``references/`` and ``docs/`` is scanned. Only
a command that states ``--config`` is checked: prose that merely mentions the
inspector, and unrelated ``--config <path>`` flags on other CLIs, are left alone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A fenced shell block spilling across lines keeps its flags on the next line,
# so the backslash continuations are folded before the flags are read.
_COMMAND_RES = (
    re.compile(r"--config\s+(\S+)"),
    re.compile(r"--server\s+(\S+)"),
)


def _folded_lines(text: str) -> list[str]:
    """Join ``\\`` continuations so a command reads as one logical line."""
    lines: list[str] = []
    for line in text.splitlines():
        if lines and lines[-1].endswith("\\"):
            lines[-1] = lines[-1][: -len("\\")] + line.lstrip()
        else:
            lines.append(line)
    return lines


def _walkthrough_files() -> list[Path]:
    return [
        *sorted(ROOT.joinpath("references").glob("*.md")),
        *sorted(ROOT.joinpath("docs").rglob("*.md")),
    ]


def test_inspector_command_resolves_a_shipped_config() -> None:
    for path in _walkthrough_files():
        text = path.read_text(encoding="utf-8")
        for line in _folded_lines(text):
            if "modelcontextprotocol/inspector" not in line:
                continue
            config_name, server_name = (rx.search(line) for rx in _COMMAND_RES)
            # Prose that only mentions the inspector states no config of its own.
            if config_name is None:
                continue
            assert server_name is not None, f"{path} names --config with no --server"

            config = ROOT / config_name.group(1)
            assert config.is_file(), (
                f"{path} points the MCP inspector at {config_name.group(1)}, "
                "which is not a file in this repository"
            )

            servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
            assert server_name.group(1) in servers, (
                f"{path} asks for server {server_name.group(1)!r}, but "
                f"{config.name} defines only {sorted(servers)}"
            )

"""Tests for ``continuum mcp install`` / ``mcp remove`` (issue #834).

The command exists for the failure states the issue reproduced: on Windows a
committed ``.mcp.json`` points at a POSIX path that does not exist, and a bare
command name is resolved by ``CreateProcess`` against the *host's* PATH, so a

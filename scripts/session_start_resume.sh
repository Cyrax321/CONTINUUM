#!/bin/sh
# SessionStart hook that runs continuum resume out of band without a model turn.
# Install as a Claude Code SessionStart hook to make detection instant.
# Global flags such as --json must precede the subcommand (issue #774).
set -e
if command -v continuum >/dev/null 2>&1; then
  continuum --json resume 2>/dev/null | head -c 2000
fi

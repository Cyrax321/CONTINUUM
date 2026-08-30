#!/usr/bin/env bash
# PreCompact helper for Claude Code and Codex.
# Forces a checkpoint at the compaction boundary and snapshots the recovery
# verdict beside the log so a resumed session can prove what was verified.
# Keep this tiny: the policy is data, not code.
set -euo pipefail
RUN_ID="${1:-${CONTINUUM_RUN_ID:-}}"
if [ -z "$RUN_ID" ]; then
  echo "usage: $0 <run_id>  or set CONTINUUM_RUN_ID" >&2
  exit 2
fi
continuum checkpoint "$RUN_ID" --reason "pre-compact" || true
continuum resume "$RUN_ID" --json > ".continuum/precompact-resume.json"
continuum verify "$RUN_ID" --json > ".continuum/precompact-verify.json"
echo "precompact checkpoint and verify snapshots written for $RUN_ID"

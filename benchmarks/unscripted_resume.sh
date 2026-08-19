#!/usr/bin/env bash
# Unscripted autonomous agent test for issue #6.
#
# Runs Claude Code (an independent LLM agent CLI) against the continuum-mcp
# server with NO step-by-step instructions, only the goal in benchmarks/task.md.
# Records the tool-call sequence so we can verify autonomous checkpoint / resume
# behaviour. Requires ANTHROPIC_API_KEY and the `claude` CLI on PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB="${CONTINUUM_DB:-continuum.db}"
RUN_ID="${RUN_ID:-run_6_demo}"
TASK_FILE="${TASK_FILE:-benchmarks/task.md}"
MAX_TURNS="${MAX_TURNS:-30}"
MODEL="${MODEL:-}"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ERROR: set ANTHROPIC_API_KEY before running this harness." >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: claude CLI not found on PATH." >&2
  exit 1
fi

CONTINUUM="${CONTINUUM:-continuum}"

# Only the continuum MCP tools are auto-approved; everything else would prompt
# and block in -p mode, keeping the run on-task and harmless.
CLAUDE_ARGS=(--print --mcp-config .mcp.json --allowedTools 'mcp__continuum__*' --max-turns "$MAX_TURNS")
[ -n "$MODEL" ] && CLAUDE_ARGS+=(--model "$MODEL")

rm -f "$DB"
"$CONTINUUM" --db "$DB" init >/dev/null

echo "=== Phase 1: autonomous run (RUN_ID=$RUN_ID) ==="
PROMPT="$(printf 'RUN_ID=%s\n\n%s' "$RUN_ID" "$(cat "$TASK_FILE")")"
claude "${CLAUDE_ARGS[@]}" --output-format stream-json "$PROMPT" \
  | tee "benchmarks/run1.stream.jsonl" \
  | grep -oE '"name":"mcp__continuum__[a-z_]+"' | sort | uniq -c || true

echo
echo "=== Phase 2: resume in a fresh session ==="
claude "${CLAUDE_ARGS[@]}" --output-format stream-json \
  "RUN_ID=$RUN_ID Resume the run above and finish any incomplete steps, then call continuum_validate." \
  | tee "benchmarks/run2.stream.jsonl" \
  | grep -oE '"name":"mcp__continuum__[a-z_]+"' | sort | uniq -c || true

echo
echo "=== CONTINUUM's ground-truth record of what the agent did ==="
"$CONTINUUM" --db "$DB" events "$RUN_ID" | tee "benchmarks/agent_trail.txt"
echo
echo "=== Resume assessment ==="
"$CONTINUUM" --db "$DB" resume "$RUN_ID" || true

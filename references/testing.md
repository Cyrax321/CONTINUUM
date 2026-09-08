# Testing everything CONTINUUM does

CONTINUUM has five integration seams and several enforcement surfaces. This
guide runs them all, from zero-setup to live-agent chaos drills. Levels 1-2
need nothing but this repository; higher levels need the named tool.

## Level 1: the full automated suite (no external services)

```bash
pip install -e ".[dev]"
pytest --tb=short -q --cov=continuum --cov-report=xml --cov-report=term-missing
ruff check src/ tests/ examples/        # lint
ruff format --check src/ tests/ examples/
mypy src/continuum                     # strict type check
```

On the current main branch, collection reports approximately 2,058 tests; the
exact count and pass/skip totals vary with Python version, platform, optional
dependencies, and external services.

What those 2,000+ tests cover without any SDK or network: event-chain
integrity and tamper detection, semantic projection, checkpoint policy and
restore, ledger claim/dedup/fail/reconcile (including cross-run unscoped
claims through the action index), validator staleness propagation, recovery
modes and their exit codes, MCP server over an in-process transport,
authorization deny-by-default, gate decision table, gateway decisions against
a folded ledger, briefing content, reconciler probe parsing and settlement,
OTel span recognition (duck-typed), thin adapters against fake frameworks,
installer idempotency for all three clients, and CLI contract details such as
NOT_FOUND on typos.

## Level 2: self-contained live machinery (stdlib only)

These run real processes and real sockets:

```bash
# The enforcing gateway end to end: denial, claim, forwarding, settlement.
python -m pytest tests/test_gateway.py -q

# Crash-recovery proofs with a real process kill and a real side effect.
python examples/crash_recovery_agent.py
python scripts/mcp_smoke.py          # real stdio JSON-RPC to the MCP server

# Recovery benchmarks: five scenarios x three strategies, duplicate counts.
continuum benchmark

# Integrity: hash-chain audit plus projection-vs-index drift check.
continuum verify <run_id> --index           # add --repair-index to rebuild

# Tamper evidence: flip a payload byte, watch verify fail, rebuild, reverify.
```

## Level 3: coding CLIs over lifecycle hooks (Claude Code / Gemini / Codex)

```bash
mkdir demo && cd demo
continuum init
continuum start demo-run --goal "<your task>"
continuum hooks install claude-code --with-gate
echo '{"tools": {"Write": {"key_template": "{file_path}"}}}' > .continuum/gate.json
claude    # then give the task; say "hi" in a fresh session to see the briefing
```

Watch from another terminal:

```bash
watch -n2 'continuum events demo-run | tail -6'
continuum status demo-run
continuum actions demo-run
```

Chaos drill: kill the CLI mid-task (`kill -9`), open a fresh session, say
"hi". Expected: SessionStart briefing reports progress, lists disk-checked
file observations as `[verified]`, and prints executable next steps. Gate
drill: remove the run's claims and ask for another file - the first Write is
denied with `continuum_intercept_action` instructions, and the model retries
after claiming.

## Level 4: MCP protocol boundary (inspector)

```bash
npx @modelcontextprotocol/inspector --cli \
  --config mcp-config.json --server continuum-mcp \
  --method tools/list --format json
```

Drive `record_progress` -> `checkpoint` -> `resume` sequences through real
subprocesses so every crash between calls is a genuine process death. The
deny-by-default allowlist and the anti-self-certification verdicts
(`request_human`) are what to assert.

## Level 5: framework adapters with a live model

```bash
export OPENROUTER_API_KEY=...   # never written to disk
python examples/langchain_real_llm.py       # soft resume: exactly-once proof
python examples/langgraph_real_llm_crash.py # hard crash: uncertain + blocked
python examples/openai_real_llm.py
python examples/multitool_real_llm.py       # multi-tool orchestration demo
```

Each adapter also has SDK-free tests (duck-typed stand-ins) that always run:
`tests/test_integration_*.py`, `tests/test_adapters_thin.py`.

## Level 6: OpenTelemetry bridge

```bash
pip install opentelemetry-sdk
python -m pytest tests/test_otel.py          # processor test unskips
```

In a traced application, register `make_span_processor(storage)` on your
TracerProvider; spans carrying `gen_ai.tool.name` (or the other recognised
keys) land in the active run's log as TOOL_COMPLETED/TOOL_FAILED.

## Level 7: the enforcing gateway against a real upstream

Point a route at any reachable API, start `continuum gateway --port 8765`,
claim via `continuum_intercept_action` (MCP) or `ActionLedger.claim`, then:

- unclaimed POST -> 403 with claim instructions
- claimed POST -> forwarded; response settles the claim
  (2xx completes, 4xx fails-certain, 5xx/network fails-uncertain)
- repeat of a completed call -> refused as a duplicate

## Attestation (optional, tamper proofing beyond evidence)

```bash
continuum attest-keygen --out signer.pem
continuum attest <run_id> --key signer.pem
continuum attest-verify <run_id> --attest <file>
```

## What "all green" means

| Level | Green looks like |
|:--|:--|
| 1 | pytest/ruff/format/mypy clean |
| 2 | benchmark shows 0 duplicates; verify ok; crash examples print resumed state |
| 3 | fresh session briefs unprompted; observations `[verified]`; gate denials teach then pass |
| 4 | inspector lists eleven tools; mutating calls honour the allowlist |
| 5 | exactly-once holds across soft resume and hard crash for every adapter |
| 6 | tool spans appear in `continuum events` with `via: otel` |
| 7 | 403 -> claim -> forward -> settled, all visible in the event chain |

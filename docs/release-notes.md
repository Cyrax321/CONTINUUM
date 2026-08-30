# CONTINUUM 0.1.0 - Release notes skeleton

> This is the skeleton the launch post quotes from. It is checked in so the post
> does not drift from the tagged tree. Fill the bracketed placeholders at tag
> time, keep the headings, and do not invent a number the suite does not already
> print. See `docs/research.md` for the backing suite and design-doc list.

---

## CONTINUUM 0.1.0 - Verifiable semantic recovery for long-running agents

**Tag:** `v0.1.0`  ·  **Date:** `[YYYY-MM-DD]`  ·  **PyPI:** `continuum-agent==0.1.0`  ·  **Image:** `ghcr.io/cyrax321/continuum:0.1.0`

One line: semantic checkpoints (not conversation dumps), an idempotent action
ledger that refuses duplicate side effects, and a hash-chained event log, all
behind a deny-by-default MCP server.

### Highlights (what this tag ships)

- **Semantic checkpoints and projection** - compact, versioned `SemanticState` folded from a hash-chained event log (`src/continuum/events.py`, `src/continuum/state/semantic.py`, `src/continuum/storage/sqlite.py`, `src/continuum/checkpoint/manager.py`). See `docs/recovery_walkthrough.md`.

- **Independent environment revalidation** - every checkpoint component verified against the current world before resume, staleness propagates `dependency -> evidence -> finding -> decision` (`src/continuum/state/validator.py`, `src/continuum/provenance_map.py`).

- **Provenance-aware, no self-certification** - every fact traces to its `Origin`; agent-reported progress resolves to `REQUIRES_REVIEW` until `REVIEW_CONFIRMED` (`src/continuum/provenance_map.py`, `src/continuum/state/validator.py`). Fix `9738b9e`.

- **Idempotent side effects, reconciled from reality** - claim-before-fire ledger, `UnknownSideEffect` never guessed, reconciled by probes (`src/continuum/actions/ledger.py`, `src/continuum/gate.py`, `src/continuum/gateway.py`, `src/continuum/replayguard.py`).

- **Recovery as a sealed contract** - `RecoveryEngine.assess` reduces three signals to one `RecoveryMode` (max-severity, `RESUME < ... < ABORT`) and returns a hash-sealed `RecoveryContract` with `evidence` / `reason` / `next_allowed_action` / `human_steps` (`src/continuum/recovery/`).

- **Deny-by-default surfaces** - MCP server (11 tools, read-only/mutating split, allowlist + token auth in `src/continuum/mcp/authz.py`), CLI with exit-code contract (`src/continuum/cli/main.py`), enforcing HTTP gateway (`src/continuum/gateway.py`), OTel bridge (`src/continuum/otel.py`), observation hooks + briefing (`src/continuum/hooks.py`, `src/continuum/clienthooks.py`).

- **Adapters and thin hooks** - nine class-based adapters in `src/continuum/adapters/` plus thin hook surfaces `src/continuum/adapters/thin.py` (CrewAI, AutoGen, Pydantic AI) and `make_continuum_checkpointer` for LangGraph native persistence. See `references/adapters.md` and `docs/recipes/`.

- **Hardening around the core** - version pinning (`src/continuum/pinning.py`), replay-or-fork (`src/continuum/replay_similarity.py`), retry budgets (`src/continuum/budgets.py`), log compaction and archive (`src/continuum/storage/` schema v5+), action index, multi-agent parent/child (`src/continuum/recovery/family.py`), fork semantics, informed retry, consumed-grant tracking, Ed25519 attestation (`continuum attest`).

For the full change list since the repository started, see `CHANGELOG.md` section `[0.1.0]`.

### How to try it in two minutes

Zero-install paths (no clone, no publish):

```bash
pip install continuum-agent==0.1.0
continuum --help
continuum-mcp --help
```

Or without installing:

```bash
docker run --rm ghcr.io/cyrax321/continuum
docker run --rm ghcr.io/cyrax321/continuum continuum --help
uvx --from git+https://github.com/Cyrax321/CONTINUUM.git continuum --help
```

With a checkout:

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM
uv pip install -e ".[dev]"
continuum --help
pytest -q
```

The Docker image and Codespace are built by CI on every push to `main` and every release tag (`.github/workflows/docker-publish.yml`). See `references/install.md` for extras and verification.

#### Wire a coding agent (optional)

```bash
continuum start my-task --goal "What the agent should do"
continuum hooks install claude-code --with-gate   # also: gemini, codex
```

From then on every file the agent writes becomes digest-verified evidence, its session starts with a deterministic briefing, unclaimed side effects are refused before they fire, and a crash resumes with executable next steps. No prompt file required.

### Crash recovery, for real (regenerable visual)

`python demo-run/generate_crash_visual.py` runs `demo-run/worker.py` until a real `os._exit(9)` at document 399 mid-batch, calls `continuum resume --env dataset=v4` and correctly refuses (`REQUEST_HUMAN`, `safe:false`, non-zero exit), reconciles the uncertain `github.create_issue`, then resumes from the same `demo-run/agent.db` and finishes. Both paths are shown, not only the happy path.

- Visual: `docs/assets/crash-recovery.svg` (plain transcript: `docs/assets/crash-recovery.txt`)
- Regenerate: `python demo-run/generate_crash_visual.py` - or `python scripts/generate_crash_visual.py`
- Walkthrough: `docs/recovery_walkthrough.md` (`examples/recovery_walkthrough.py`)

![Crash recovery: hard kill, refusal, reconcile, resume](docs/assets/crash-recovery.svg)

Sample end-of-run audit printed by the harness (from this tree, not estimated):

```
documents processed      1000
duplicates               0
GitHub issues created    1
progress recovered       1000/1000
event chain verified     True

No work repeated. No side effect duplicated.
```

This block comes from `examples/crash_recovery_agent.py` and from `demo-run/generate_crash_visual.py`. If you need a number, run the script that prints it.

### Verification

Three places to start, all of which are real runs, not prose:

- `python examples/recovery_walkthrough.py` - one failure from adapter error to sealed contract (output matches `docs/recovery_walkthrough.md`).
- `python examples/crash_recovery_agent.py` - the hard-kill harness above (`os._exit(9)`, real side effect on disk, `verify_events().ok`).
- `python scripts/mcp_smoke.py` - drives `continuum-mcp` over stdio JSON-RPC and asserts `proceed:false` on duplicate intercept.

Bench harness: `continuum benchmark` (minimal harness, five scenarios plus drift check, see `references/bench.md`). Fault-injection and horizon suites live in `benchmarks/fault_injection/` and `src/continuum/benchmark/phase6/` and emit the shared envelope `{benchmark, generated_at, summary, results}` (see `docs/research.md`). No numbers are quoted here.

### What CONTINUUM is not

Not an LLM, not an agent framework, not a workflow engine, not a vector database, and not a RAG system. See `README.md` section [What CONTINUUM Is Not](../README.md#what-continuum-is-not). The core abstraction is `semantic state + environment validation + action reconciliation = safe recovery`.

### Where CONTINUUM sits

See `README.md` section [Where CONTINUUM sits](../README.md#where-continuum-sits) - a four-row orientation table (harness / durable execution / control plane / verification substrate) where every claim traces to a module path or a published suite that already prints it. Backing index: `docs/research.md`.

### Status and honest limitations

- Tests are enforced on Python 3.11, 3.12, 3.13 (see `STATUS.md` for the `pytest` / `ruff` / `mypy` gate and the audit row). Count varies by environment (Postgres without `CONTINUUM_TEST_POSTGRES_DSN`, adapter extras absent), so `README.md` keeps the wording `~1,380 collected (exact count and skips vary by environment)` as #316 requires.

- MCP caller identity is `clientInfo.name` by default; set `CONTINUUM_MCP_TOKEN` (and optionally `CONTINUUM_MCP_CLIENT_TOKENS` / `CONTINUUM_MCP_CONFIRM_TOKEN`) for shared-secret auth. See `STATUS.md` and `docs/api/mcp.md`.

- Gate does not see inside shell commands (Bash/curl bypass structured-tool claims). Postgres is CI-tested but not battle-tested. One level of multi-agent hierarchy (v1). Payload offloading (#254) is not yet implemented. Cloud API (Phase 13) is not started beyond storage + sidecar transport.

See `STATUS.md` for the verified vs believed breakdown and open correctness bugs.

### Roadmap

`README.md` section [Roadmap](../README.md#roadmap) and `docs/UPGRADE_SPEC.md` (phased plan through 0.1.0 and beyond). The v0.1.0 tag does not block Phases 2-5, they are the post-release track per `docs/CONTINUUM_MASTER_PLAN.md`.

### Contributors and sponsor

Contributors are listed in `README.md` section [Contributors](../README.md#contributors) and in `docs/contributors/`. Sponsor: `https://github.com/sponsors/Cyrax321`.

### Release checklist

The maintainer steps for cutting `v0.1.0` are in `docs/release-checklist.md` (issue #388 / #405). Tagging is maintainer-only.

---

#### Checklist for the person cutting the post (not part of the published notes)

- [ ] Replace `[YYYY-MM-DD]` with the tag date and confirm `CHANGELOG.md` heading `## [0.1.0] - YYYY-MM-DD` exists exactly once.
- [ ] Confirm `pyproject.toml` `version = "0.1.0"` and `src/continuum/__init__.py` `__version__ = "0.1.0"` match the tag `v0.1.0`.
- [ ] Run the full gate on `main` and confirm CI is green: `pytest`, `ruff check`, `ruff format --check`, `mypy src/continuum`.
- [ ] Regenerate the visual and confirm it still shows the refusal branch: `python demo-run/generate_crash_visual.py` (check `docs/assets/crash-recovery.txt` contains `REQUEST_HUMAN` and two `exit code` lines).
- [ ] Verify the wheel and Docker image after tagging: `uv build` + clean-venv smoke, `docker pull ghcr.io/cyrax321/continuum:0.1.0` + `docker run --rm ghcr.io/cyrax321/continuum`.
- [ ] Link `docs/research.md`, `docs/assets/crash-recovery.svg`, and `examples/crash_recovery_agent.py` from the GitHub Release description.

This skeleton deliberately carries no CLI command table (that is #363) and no harness hook recipes (that is `docs/recipes/` via #396). Keep it that way.

<p align="center">
  <img src="docs/assets/readme-img.png" alt="CONTINUUM Banner" width="100%" />
</p>

<p align="center">
  <strong>CONTINUUM: Verifiable semantic recovery for long-running AI agents.</strong>
  Semantic checkpoints (not conversation dumps), an idempotent action ledger
  that refuses duplicate side effects, and a hash-chained tamper-evident event
  log, all exposed as a deny-by-default MCP server. Framework-agnostic,
  Python 3.11+.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://pypi.org/project/continuum-agent/"><img src="https://img.shields.io/pypi/v/continuum-agent?style=flat-square&label=PyPI" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=flat-square" alt="License" /></a>
  <a href="https://pydantic.dev"><img src="https://img.shields.io/badge/pydantic-v2-E92063?style=flat-square&logo=pydantic&logoColor=white" alt="Pydantic v2" /></a>
  <a href="https://continuum-nu-six.vercel.app/"><img src="https://img.shields.io/badge/website-live_demo-E06D53?style=flat-square" alt="Website Demo" /></a>
  <a href="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml"><img src="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml/badge.svg" alt="CI status" /></a>
  <a href="https://app.codecov.io/gh/Cyrax321/CONTINUUM"><img src="https://img.shields.io/codecov/c/github/Cyrax321/CONTINUUM?style=flat-square&logo=codecov" alt="Coverage" /></a>
  <a href="https://github.com/sponsors/Cyrax321"><img src="https://img.shields.io/badge/sponsor-❤-ff69b4?style=flat-square&logo=githubsponsors" alt="Sponsor" /></a>
  <a href="https://app.ona.com/#https://github.com/Cyrax321/CONTINUUM"><img src="https://ona.com/build-with-ona.svg" alt="Build with Ona" /></a>
</p>

<p align="center">
  <a href="https://continuum-nu-six.vercel.app/"><strong>Visit the CONTINUUM website</strong></a>
</p>

---

## Contents

[Why](#why) · [Quick Start](#quick-start) · [How it works](#how-it-works) · [Where CONTINUUM sits](#where-continuum-sits) · [Features](#features) · [Security Extension](#security-extension) · [Empirical Verification](#empirical-verification) · [MCP Integration](#mcp-integration) · [Framework Integration](#framework-integration) · [Core Concepts](#core-concepts) · [Architecture](#architecture) · [API and CLI](#api-and-cli) · [Roadmap](#roadmap) · [What CONTINUUM Is Not](#what-continuum-is-not) · [Related work](#related-work) · [Status and limitations](#status-and-limitations) · [Contributing](#contributing) · [License](#license)

---

## Why

Modern AI agents run long tasks (hundreds of LLM calls, tool invocations, file and database writes). When they crash, the usual response is to replay everything from scratch, which duplicates work, duplicates side effects, wastes tokens, and loses decisions.

CONTINUUM asks a narrower, harder question: can an agent resume from a compact semantic representation of its task state while independently verifying that state is still valid in the current environment? Its differentiator is three-part:

- **Semantic checkpoints**: a compact, versioned representation of what the agent needs to continue, not a conversation dump.
- **Independent environment revalidation**: every checkpoint component is verified against the current environment before resume, with staleness propagating through the dependency graph.
- **Provenance-aware state**: every fact traces to its origin, so agent-reported progress is never self-certifying.

## Quick Start

Published to PyPI as `continuum-agent` 0.1.0 — `pip install continuum-agent` (`pip install continuum-agent==0.1.0` to pin). Release tags additionally ship built wheels attached to [GitHub Releases](https://github.com/Cyrax321/CONTINUUM/releases).

Zero-setup paths (no clone, no install, nothing published anywhere):

| Path | How |
|:--|:--|
| Install from PyPI | `pip install continuum-agent==0.1.0` — then `continuum --help` |
| Watch crash recovery happen end to end | `docker run --rm ghcr.io/cyrax321/continuum` |
| Use the CLI through Docker | `docker run --rm ghcr.io/cyrax321/continuum continuum --help` |
| Run the CLI without cloning | `uvx --from git+https://github.com/Cyrax321/CONTINUUM.git continuum --help` |
| Full dev environment in the browser | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Cyrax321/CONTINUUM?quickstart=1) |

The Docker image is published to GHCR by CI on every push to `main` and every release tag (`.github/workflows/docker-publish.yml`). The Codespace is defined in `.devcontainer/`.

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

uv venv && source .venv/bin/activate     # macOS / Linux; Windows: .venv\Scripts\activate

# Contributors (recommended): library + CLI + all test tooling + every adapter
uv pip install -e ".[dev]"

# Or pick only what you need: . (minimal), [mcp], [otel], [langgraph],
# [openai], [langchain], [attest], [postgres]

# Or skip the clone entirely:
uv pip install git+https://github.com/Cyrax321/CONTINUUM.git
uv pip install "continuum-agent[mcp] @ git+https://github.com/Cyrax321/CONTINUUM.git"
```

> **pip fallback:** replace `uv pip install` with `pip install` in every command above.

Verify:

```bash
continuum --help                 # CLI entrypoint
continuum-mcp --help             # MCP server entrypoint (needs [mcp] or [dev])
pytest -q                        # ~1,380 collected (exact count and skips vary by environment)
ruff check src/ tests/ examples/ && ruff format --check src/ tests/ examples/
mypy src/continuum               # the three gates CI enforces
```

The core library has one runtime dependency (`pydantic>=2.7`); everything else is opt-in. The full package map, extras matrix, Postgres test setup, and per-command verification are in [references/install.md](references/install.md).

### Wire a coding agent in two minutes

For Claude Code, Gemini CLI, or Codex, you do not write Python and do not need a prompt file:

```bash
continuum start my-task --goal "What the agent should do"
continuum hooks install claude-code --with-gate   # also: gemini, codex
```

From then on every file the agent writes is captured as hash-chained evidence, its session starts with an automatic status briefing, unclaimed side effects registered in `.continuum/gate.json` are refused before they fire, and a fresh session after any crash resumes with executable next steps. No CLAUDE.md required.

Minimal library example, record and recover:

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="Analyze 10,000 documents"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "Analyze 10,000 documents", "total": 10_000})

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})

# After a crash, a new process picks up exactly where it stopped:
state = project("run_4821", store.read_events("run_4821"))
print(state.progress.completed)            # already done, not repeated
print(store.verify_events("run_4821").ok)  # True, chain intact after the crash
```

**Run the proof yourself:**

```bash
python examples/crash_recovery_agent.py   # real process kill, real side effect
python examples/context_compaction.py     # transcript lost, checkpoint survives
python examples/model_switch.py           # Model A dies, Model B resumes safely
python scripts/mcp_smoke.py               # real subprocess, real JSON-RPC traffic
```

The `e2e-autonomy-test/` kit scripts a real invoice-batch task, a hard-kill mid-run, and a fresh resume session, then scores the outbox, ledger, and event chain out of band. Run 1 scored **7/7 mechanics** against a real Claude Code session. Full walkthrough in [references/e2e.md](references/e2e.md).

## How it works

CONTINUUM separates **LLM context** (temporary) from **durable task state** (permanent). Instead of saving conversation history, it constructs a semantic checkpoint, the minimum verified information required to continue.

![CONTINUUM how it works](docs/assets/architecture.svg)

The detailed explanation, the projection model, and the recovery context are in [references/architecture.md](references/architecture.md).

## Where CONTINUUM sits

Four concerns overlap in every long-running agent. CONTINUUM owns only the last one and touches the other three through explicit seams. No competitor is named and no claim is made without a shipped module or a published suite that already prints it.

| Layer | Answers | How it connects (shipped modules or published output) |
|:--|:--|:--|
| Harness | How does the agent call tools and make progress toward a goal? | Outside CONTINUUM. Wiring points ship in `src/continuum/adapters/generic.py` (`GenericAgentAdapter`), `src/continuum/adapters/thin.py` (CrewAI, AutoGen, Pydantic AI hooks), `src/continuum/mcp/server.py` (MCP stdio), `src/continuum/hooks.py` and `src/continuum/clienthooks.py` (coding-CLI lifecycle hooks), `src/continuum/gateway.py` (enforcing HTTP proxy for any language), and `src/continuum/otel.py` (OpenTelemetry bridge). Recipes are in `docs/recipes/` and `references/adapters.md`. |
| Durable execution | What happened before a crash and what can be replayed without losing work? | Hash-chained event log `src/continuum/events.py` with `verify()` and `trusted_through`, durable storage `src/continuum/storage/sqlite.py` (WAL, `synchronous=FULL`, schema v6) and `src/continuum/storage/postgres.py` plus `src/continuum/storage/migrations.py`, policy-driven checkpoints `src/continuum/checkpoint/manager.py` and `src/continuum/checkpoint/policy.py` that replay the gap on `restore()`. Walkthrough is in `docs/recovery_walkthrough.md` (output of `examples/recovery_walkthrough.py`). |
| Control plane | Which run is active, who may act on it, and where does output go? | Run registry and parent/child hierarchy `src/continuum/storage/` and `src/continuum/recovery/family.py` (`continuum tree`), allowlist authz `src/continuum/mcp/authz.py` (`CONTINUUM_MCP_MUTATING_CLIENTS` / `CONTINUUM_MCP_TOKEN`), presentation surfaces `src/continuum/dashboard/app.py` and `src/continuum/serve/server.py`, CLI `src/continuum/cli/main.py` (`continuum runs`, `continuum tree`, `continuum health`). |
| Verification substrate | Given the checkpoint at time T and the world as it is now, is it still safe and correct to continue? | `src/continuum/state/validator.py` (staleness `dependency -> evidence -> finding -> decision` plus `PlanStep.depends_on`), `src/continuum/provenance_map.py` (`Origin` to `REQUIRES_REVIEW` until `REVIEW_CONFIRMED`), `src/continuum/actions/ledger.py` with `src/continuum/actions/idempotency.py` and `src/continuum/gate.py` / `src/continuum/gateway.py` (claim-before-fire, refuses duplicates, raises `UnknownSideEffect` for reconciliation), `src/continuum/replayguard.py` (portable guard), `src/continuum/pinning.py` and `src/continuum/replay_similarity.py` (replay correctness), `src/continuum/budgets.py` (retry caps), `src/continuum/recovery/engine.py` + `src/continuum/recovery/contract.py` + `src/continuum/recovery/planner.py` + `src/continuum/recovery/observations.py` (max-severity `RESUME < ... < ABORT`, sealed contract with `evidence` / `reason` / `next_allowed_action` / `human_steps`), `src/continuum/checkpoint/rewind.py` (atomic dual-state rewind), `src/continuum/analysis/prefix_trust.py` (advisory trust). Published checks: `docs/recovery_walkthrough.md`, `benchmarks/fault_injection/` (suite that prints `detection_rate` / `unsafe_resume_rate`), `src/continuum/benchmark/phase6/` (recovery-correctness suite), `docs/RESULTS.md`, and the regenerable visual below. |

Every row above is traceable to a path that exists on `main` at the tagged commit. Nothing in this table restates a benchmark number, benchmarks live only in the suite output they already print. See `docs/research.md` for the full list of published suites and design docs.

### Crash recovery, for real

The image below is not a mock. It is the output of `python demo-run/generate_crash_visual.py`, which runs `demo-run/worker.py` until `os._exit(9)` at document 399, calls `continuum resume --env dataset=v4` and shows the refusal path (`REQUEST_HUMAN`, `safe:false`, exit 20), reconciles the uncertain side effect with a probe, then resumes from the same database and finishes with no duplicate work. The transcript is also saved as `docs/assets/crash-recovery.txt` for audit.

Regenerate it:

```bash
python demo-run/generate_crash_visual.py
# or: python scripts/generate_crash_visual.py
```

![Crash recovery: hard kill mid-batch, refusal, reconcile, resume](docs/assets/crash-recovery.svg)

Full walkthrough with code is in `docs/recovery_walkthrough.md` (`examples/recovery_walkthrough.py`). The minimal bench harness is in `references/bench.md` (`continuum benchmark`).

## Features

| Capability | What it gives you |
|:--|:--|
| Semantic checkpoints | Compact, versioned, inspectable state, not a transcript dump |
| Idempotent action ledger | Refuses duplicate external side effects; surfaces uncertain ones for reconciliation |
| Environment revalidation | Every checkpoint component verified against the current world before resume |
| Provenance-aware state | Agent-reported progress is marked `REQUIRES_REVIEW`, never self-certifying |
| Recovery engine | Seven recovery modes with a deterministic, sealed next-action contract |
| Deny-by-default MCP server | Eleven tools, read-only/mutating split, caller allowlist |
| Framework adapters | Generic Python, OpenAI Agents SDK, LangGraph, and LangChain integrations |
| Secure planning loop | Two-signal observation verification escalates high-risk branches to REQUIRES_REVIEW |
| Periodic revalidation | Environment re-checked on a schedule, catching mid-run drift within one cycle |
| Tamper-evident log | Hash-chained event log (36 event types) with integrity verification |
| Enforcing gate | Unclaimed side-effect calls are refused before they fire; deny messages teach the claim protocol |
| Observation hooks | Every file a coding CLI writes becomes digest-verified evidence, outside model control |
| Session briefing | Fresh sessions learn run state deterministically at start, including the last session's reasoning summary |
| Reconciler probes | Registered commands settle uncertain side effects automatically; humans see only the rest |
| Executable guidance | Resume/validate render next steps as runnable commands, not statuses |
| Enforcing HTTP gateway | Outbound calls in any language require claims; responses settle them from reality |
| OpenTelemetry bridge | Tool-call spans from production tracing become evidence with zero code changes |
| Action index | Cross-run idempotency lookups are indexed reads, not full-log scans |
| Version pinning | Caller-asserted prompt/tool/model hashes stored per claim; drift surfaced on resume |
| Retry budgets | Per-action-type attempt caps enforced at claim time; agents see remaining attempts |
| Multi-agent parent/child | Parent resume composes family worst state; uncertain child blocks parent |
| Informed retry | Engine-authored failure summaries injected into post-recovery resumes |
| Fork semantics | Divergent continuations branch into child runs with fresh authority |
| Log compaction | Pre-anchor prefix archived verbatim; live log bounded for month-long runs |
| Consumed-grant tracking | Single-use authority references are marked spent at terminal status; reuse after restore is refused (`GRANT_DENIED`), defending the checkpoint-restore path against Authority Resurrection |
| Chain attestation | `continuum attest` signs a run's chain head with Ed25519 so an external verifier can prove history was unaltered as of a known key |
| HITL dashboard surface | Confirm/reconcile/complete buttons with audit parity to the CLI |

## Security Extension

Two additive security extensions sit on top of the recovery and checkpoint substrate. They do not change resume, replay, or the existing crash-time revalidation path.

- **Secure Planning Loop**: observations carry provenance and are verified by two independent signals (`verified` / `unverified` / `contested`). A plan branch gated on an unverified or contested observation is escalated to `REQUIRES_REVIEW`. Decisions are appended to the ledger as `PERCEPTION_OBSERVED` and `BRANCH_RESOLVED` events.
- **Periodic Revalidation**: reuses the recovery engine on a step interval (default 25) and on app switch, so mid-run environment drift is caught within one cycle instead of only at the next crash.

See [docs/PROBLEM.md](docs/PROBLEM.md), [docs/RESULTS.md](docs/RESULTS.md), and [STATUS.md](STATUS.md).

## Empirical Verification

CONTINUUM is verified against real LLM agents, live protocol boundaries, and hard process crashes, not just mock unit tests.

- **Real agents**: multi-session Claude Code invoice batches with mid-run `SIGKILL`, scored 7/7 on mechanics; resumed sessions queried `continuum_resume`, routed side effects through the two-phase ledger, refused to duplicate verified writes, and respected `request_human`. Live testing surfaced prompt-drift dedup gaps, closed by canonical path normalization and token-based fallback in `ActionLedger.claim()`.
- **Third-party clients**: Gemini CLI and Kilo Code connected over stdio JSON-RPC against the live SQLite store, validating multi-agent co-existence and authorization isolation.
- **Protocol compliance**: driven end to end with `@modelcontextprotocol/inspector --cli` across process deaths; mutating tools deny by default behind `CONTINUUM_MCP_MUTATING_CLIENTS`; external claims degrade to `REQUIRES_REVIEW` (`safe: false`).
- **Self-healing**: hard-killed servers recover from orphaned SQLite `-wal`/`-shm` sidecars via single-retry cleanup at startup.
- **Scale**: roughly 1,380 tests collected (~1,360 passing; the rest skip without optional services) on Python 3.11, 3.12, and 3.13 (unit, `hypothesis` property-based, concurrency, adversarial). CONTINUUM-Bench runs five crash scenarios plus a dedicated argument-drift scenario, measuring 0 duplicate work and 0 duplicate side effects for CONTINUUM against full duplication for naive replay; a separate 12-scenario recovery-correctness suite (`continuum.benchmark.phase6`) encodes the crash points from the durable-execution survey as executable assertions.
- **Adversarial audit**: the full MCP surface was audited over the live protocol; three defects were found and fixed. Method and reproduction steps in [test.md](test.md).

## MCP Integration

CONTINUUM ships an MCP server so an agent can record progress, checkpoint, and route external side effects through the ledger without embedding the library:

```bash
uv pip install -e ".[mcp]"
CONTINUUM_MCP_MUTATING_CLIENTS=your-client-name continuum-mcp
```

Eleven tools over stdio. Three are read-only (`continuum_validate`, `continuum_resume`, `continuum_list_actions`); eight mutate. Side effects are two-phase (claim, perform, complete), and mutating tools deny by default behind an allowlist. Agent-reported state is recorded with `Origin.EXTERNAL_AGENT` provenance and marked `REQUIRES_REVIEW`.

Verification details, including crash recovery at startup and the end to end Claude Code test, are in [references/mcp.md](references/mcp.md). If a registered server reports `CONNECTION_CLOSED`, the cause is almost always `PATH` resolution rather than the server itself: [docs/api/mcp.md](docs/api/mcp.md#troubleshooting) has the diagnosis and two remedies.

## Framework Integration

Nine adapters ship in `src/continuum/adapters/` (one in-process facade plus eight integrations), all optional installs so the core stays standard-library-only:

| Adapter | Class | Notes |
|:--|:--|:--|
| Generic Python agent | `GenericAgentAdapter` | In-process facade; writes trusted (`Origin.DETERMINISTIC`) state. |
| Filesystem sandbox | `FilesystemSandboxAdapter` | Local directory sandbox, no external service, default for docs and CI. |
| Python in-process | `PythonInProcAdapter` | Runs Python in a temp workdir, records via ledger. |
| Container | `ContainerAdapter` | Docker backed, guarded skip when `docker` is absent. |
| Browser | `BrowserAdapter` | Playwright backed, guarded skip when not installed. |
| Kubernetes | `KubernetesAdapter` | `kubectl` backed, guarded skip when not configured. |
| OpenAI Agents SDK | `OpenAIAgentAdapter` | Experimental. Hooks `ToolContext` / `RunHooks`; optional `openai-agents`. |
| LangGraph | `LangGraphAgentAdapter` | Experimental. Wraps a `StateGraph`; optional `langgraph`. |
| LangChain | `LangChainAgentAdapter` | Experimental. Drops `checkpoint_node` into an LCEL `Runnable` pipeline and the `create_agent` tool-calling loop; optional `langchain`. |

Each adapter records progress through the ledger and routes external effects through the two-phase intercept/complete protocol. All three framework adapters have end-to-end integration tests and have been driven against a **live OpenRouter model**, where the runs surfaced and then closed an LLM argument-drift dedup gap and two OpenAI-adapter bugs, including a live hard-crash (`os._exit(137)` mid-side-effect) proof per adapter. Full usage, live-model results, and runnable examples for every adapter are in [references/adapters.md](references/adapters.md).

Production LangGraph apps can also keep their native persistence API: `make_continuum_checkpointer(storage)` implements LangGraph's `BaseCheckpointSaver` over CONTINUUM's storage, so every put lands in the same hash-chained, provenance-tagged event log (see [references/adapters.md](references/adapters.md)).

Three further production frameworks are covered by thin, SDK-free hook surfaces in [`adapters/thin.py`](src/continuum/adapters/thin.py):

| Framework | Interception surface | Entry point |
|:--|:--|:--|
| CrewAI | global before/after tool-call hooks | `install_crewai_hooks(storage, run_id)` |
| AutoGen core | `FunctionTool.run_json` wrapped in place | `wrap_autogen_tool(tool, storage, run_id)` |
| Pydantic AI | async Hooks capability | `Agent(capabilities=[wrap_pydantic_ai_hooks(storage, run_id)])` |

For stacks none of these reach: `continuum gateway` enforces claims on outbound HTTP from any language, `continuum.otel.make_span_processor(storage)` turns existing OpenTelemetry tool spans into evidence, and `continuum serve` exposes the same operations as the MCP tools over a language-agnostic JSON wire protocol (stdio, or HTTP via `--transport http` with `CONTINUUM_SERVE_TOKEN` auth).

### Resuming agent- or MCP-reported runs

State reported over MCP, or through the OpenAI adapter, carries `Origin.EXTERNAL_AGENT` provenance and resolves to `request_human` until confirmed. LangGraph and LangChain runs use `Origin.DETERMINISTIC` and resume directly. To clear review and resume:

```bash
continuum confirm <run_id>   # records REVIEW_CONFIRMED, then re-assesses
continuum resume <run_id>    # now reports RESUME
```

Over MCP the equivalent is the `continuum_confirm` tool followed by `continuum_resume`. Confirmation is a one-time, human-attested event: the escape hatch for the self-certification safety, so an externally-driven run is never permanently stuck.

## Core Concepts

The deep reference for each concept lives in [references/concepts.md](references/concepts.md).

- **Semantic Checkpoints** - a compact, versioned representation of what the agent needs to continue.
- **State Validation** - every component independently verified; staleness propagates through the dependency graph.
- **Idempotent Action Ledger** - external side effects tracked and de-duplicated; uncertain outcomes raise instead of silently retrying.
- **Recovery Modes** - `RESUME`, `REPAIR_AND_RESUME`, `ROLLBACK`, `WAIT`, `REQUEST_HUMAN`, `ABORT` (plus `REPLAN`).
- **Recovery Contract** - a deterministic, integrity-sealed, gated next action.

## Architecture

CONTINUUM is organised around one invariant: **every fact carries its origin, and trust is earned, never assumed.** The system has five layers, five integration seams, and three guarantees.

### The three guarantees

1. **No self-certification.** Agent-reported state is marked EXTERNAL_AGENT and degrades to human review on resume. Only trusted writers (in-process adapters, CLI operators) produce DETERMINISTIC state.
2. **Side effects require claims.** External effects are claimed in an idempotent ledger before they fire; unclaimed effects are blocked at the harness boundary.
3. **Recovery decisions verify against reality.** Resume contracts check checkpoint state against the current environment (dependency versions, file digests, model identity) before declaring safety.

### Five integration seams

Any agent harness connects through exactly one of these; no framework cooperation is required.

```text
Seam 1: In-process adapters     GenericAgentAdapter.intercept_action(...);
         Python frameworks       wrap_tool(key_fn=...) on LangChain/LangGraph,
                                 OpenAI Agents SDK hooks
Seam 2: MCP server              continuum-mcp (11 tools over stdio)
         MCP-capable clients
Seam 3: CLI lifecycle hooks     continuum hooks install <client> [--with-gate]
         Coding CLIs             claude-code, gemini, codex
Seam 4: Enforcing HTTP gateway  continuum gateway --port N
         Any language            routes: .continuum/gateway.json
Seam 5: OpenTelemetry bridge    make_span_processor(storage)
         Traced applications     spans -> TOOL_COMPLETED evidence
```

### Enforcement pipeline

The gate-to-observe pipeline closes the durability gap at the harness boundary:

```text
PreToolUse hook                    PostToolUse hook
    |                                    |
    v                                    v
continuum gate                    continuum observe
    |                                    |
    |-- no claim? DENY (exit 2)          |-- TOOL_COMPLETED event:
    |   + instructions to claim          |     path, bytes, sha256
    |                                    |
    |-- live claim? ALLOW                |-- disk-checked status:
    |                                    |     verified / changed / missing
    v
agent performs effect
    |
    v
continuum_complete_action
    |
    v
claim settled from reality
```

### Recovery decision tree

The recovery engine evaluates signals in severity order and returns the maximum:

```text
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

Every resume produces a sealed contract with: recovery status, verified/invalidated components, executable next steps (`human_steps`), post-checkpoint observations (disk-checked), pinning drift, and family aggregation (multi-agent).

### Storage architecture

Schema v6. SQLite is primary; Postgres is CI-verified.

| Table | Purpose |
|:--|:--|
| `events` | Hash-chained append-only log (36 event types) |
| `runs` | Run metadata with parent_run_id for multi-agent |
| `versions` | SemanticState snapshots per checkpoint |
| `checkpoints` | Sealed checkpoint records |
| `action_index` | Cross-run idempotency projection (schema v3+) |
| `events_archive` | Compacted prefix storage (schema v5+) |
| `lg_checkpoints` / `lg_writes` | LangGraph native persistence (schema v4+) |

### Module map

CONTINUUM is one library (`src/continuum`, 104 modules) plus a large test suite (98 test files, ~1,380 tests). All modules append to and replay one hash-chained event log:

| Module | Role |
|:--|:--|
| `events.py` | Append-only, hash-chained event log and `verify()` |
| `state/` | Projection, validation, extraction |
| `storage/` | SQLiteStorage (v6 schema), postgres.py, migrations.py, actionindex.py |
| `actions/` | Idempotent action ledger, reconciliation, claim/complete, consumed-grant tracking |
| `checkpoint/` | Policy-driven checkpoints with forced anchoring |
| `recovery/` | Engine, planner, sealed contract, guidance, observations, family rollup, fork semantics, informed retry summaries |
| `gate.py` | Pre-tool-use enforcement: allow/deny against ledger claims |
| `gateway.py` | Enforcing HTTP proxy: claim-before-fire for outbound requests |
| `replayguard.py` | Portable replay-safety guard: evaluate/protected_call/langgraph_protected_node |
| `hooks.py` | Shared checkpoint hooks (auto-checkpoint, file-derived progress) |
| `clienthooks.py` | Client installer profiles and hook command management |
| `budgets.py` | Retry budget registry and evaluation |
| `pinning.py` | Version pinning normalisation and drift detection |
| `replay_similarity.py` | Semantic similarity backends (exact/fuzzy/embedding) |
| `reconcilers.py` | Probe registry for automatic settlement |
| `adapters/` | 9 class-based adapters + thin hooks (CrewAI/AutoGen/Pydantic AI) + LangGraph store |
| `mcp/` | 11 stdio tools plus authz (token auth, allowlist, confirmation token) |
| `serve/` | Sidecar (stdio JSON wire + HTTP transport) |
| `dashboard/` | Web dashboard with HITL buttons (confirm/reconcile/complete) |
| `cli/` | 33 argparse commands, exit codes as verdict |
| `otel.py` | OpenTelemetry span processor bridge |

### Honest limitations

- Gate does not see inside shell commands (Bash/curl bypass structured-tool claims)
- Postgres backend is CI-tested but not battle-tested in production
- No webhook-out for request_human notifications yet
- One level of multi-agent hierarchy v1
- Payload offloading (#254) not yet implemented

Full reference in [references/architecture.md](references/architecture.md).

## API and CLI

Python surface (`EventType`, `Run`, `SQLiteStorage`, `diff_states`, `project`) and the adapter API are documented with runnable examples in [references/api.md](references/api.md). The CLI is the same surface in shell form:

```bash
continuum runs                                   # list runs
continuum inspect <run_id>                       # semantic state
continuum validate <run_id> --env dataset=v4     # validate, read-only
continuum resume <run_id> --env dataset=v4       # recovery decision + contract + next steps
continuum checkpoint <run_id>                    # force a checkpoint, mutates
continuum actions <run_id>                       # external side effects
continuum reconcile <run_id>                     # settle uncertain effects with probes
continuum complete <run_id>                      # close a run as done, from the keyboard
continuum verify <run_id>                        # re-audit the event hash chain
continuum budget <run_id>                        # retry-budget usage per action type
continuum compact <run_id>                       # archive pre-anchor log prefix
continuum tree <parent_run_id>                   # show parent + children with recovery states
continuum attest <run_id> --key signer.pem       # sign the chain head for an external verifier
```

All wiring is host-side; the model's cooperation is optional:

```bash
continuum hooks install claude-code --with-gate   # coding CLIs: evidence, briefing, gate
continuum gateway --port 8765                     # enforcing HTTP proxy for everything else
provider.add_span_processor(continuum.otel.make_span_processor(storage))  # OTel to evidence
continuum-mcp                                     # anything MCP-capable: the eleven-tool server
continuum briefing                                # session-start context injection
continuum budget <run_id>                         # retry-budget usage report
continuum tree <parent_run_id>                    # multi-agent hierarchy view
```

Optional registries live beside your code and are data, not code: `.continuum/gate.json` (side-effect tools + stable-key templates), `.continuum/reconcilers.json` (probes that check external systems), `.continuum/gateway.json` (upstream routes).

Every command accepts `--json`, and read-only commands never write, so they are safe against a live database while an agent is mid-run. Exit codes are a safety contract (only a verified-safe run exits 0). Full command list, exit-code table, and state-diff output in [references/cli.md](references/cli.md).

## Roadmap

| Phase | Component | Status |
|:-----:|:--|:--|
| 1-11 | Data models, semantic state, persistence, checkpointing, validation, action ledger, recovery engine, CLI, crash-recovery examples, environment snapshots/diffs, framework adapters | Complete |
| 12 | Benchmark suite (CONTINUUM-Bench) | Complete (minimal harness) |
| 13 | Cloud API (FastAPI + PostgreSQL) | Partial: the PostgreSQL storage backend and the HTTP sidecar transport (`continuum serve --transport http`) are shipped and CI-tested; the hosted multi-tenant service is not started |
| 14 | Dashboard | Complete (`continuum dashboard`) |
| 15+ | Enforced durability: observation hooks, gate, session briefing, reconciler probes, enforcing gateway, OTel bridge, action index, executable guidance, multi-client installers, semantic replay detection, version pinning, retry budgets, log compaction, HITL surface, fork semantics, informed retry, multi-agent aggregation | Complete (see issue #213) |
| Next | Months-scale durability plane: milestone-anchored plans (#312), structured attempt memory (#313), atomic dual-state rewind (#292), public recovery-correctness benchmark (#293), webhook-out notifications (#305) | Planned (draft spec in [docs/UPGRADE_SPEC.md](docs/UPGRADE_SPEC.md)) |

Beyond the original plan: the MCP server, MCP authorization and caller-authentication layers, provenance and anti-self-certification, community files, schema versioning with forward migrations, a bounded recovery context, consumed-grant tracking, Ed25519 event-chain attestation, the native LangGraph checkpointer, and wheel artifacts on every push to `main` are shipped. See [STATUS.md](STATUS.md) for the verified-vs-believed breakdown and open correctness bugs.

## What CONTINUUM Is Not

| Not this | This instead |
|:--|:--|
| An LLM | A reliability layer for agents that use LLMs |
| An agent framework | A recovery layer that plugs into any framework |
| A vector database | Structured semantic state, not embeddings |
| A RAG system | Verified checkpoints, not retrieval-augmented memory |
| A workflow engine | A recovery layer, not an orchestrator |

The core abstraction: `semantic state + environment validation + action reconciliation = safe recovery`.

## Related work

CONTINUUM sits at the overlap of durable execution, idempotent side-effect tracking, and crash recovery for LLM agents. The closest neighbors are machine-checked resume contracts (Khan 2026), agentic transaction processing with constraint-gated admission (Mnemosyne 2026), checkpoint-rollback attack analysis (ACRFence 2026), and design-level prompt-injection defense (CaMeL 2025). The full annotated list, foundations, and citation audit are in [references/related-work.md](references/related-work.md).

## Status and limitations

- **Tested**: 1,360 passed + 23 skipped in a full run at the 2026-08-24 audit of this tree; CI enforces the suite on Python 3.11, 3.12, and 3.13, and counts vary by platform and optional services such as Postgres (see [STATUS.md](STATUS.md)). The MCP surface has also been audited adversarially over the live protocol; see [test.md](test.md).
- **On PyPI as `continuum-agent` 0.1.0** (`pip install continuum-agent`; clone still works via `pip install .` see Quick Start).
- **MCP caller authentication is opt-in per deployment.** When `CONTINUUM_MCP_TOKEN` is set, the server refuses every mutating tool unless the caller presents that shared secret in the `initialize` handshake's `_meta.authToken`; per-caller secrets are available via `CONTINUUM_MCP_CLIENT_TOKENS` (`name:secret` pairs). Without any token configured, authorization is by declared identity only (the historical default, preserved for local single-user use).
- **Confirming self-reported state over MCP requires a separate secret.** `continuum_confirm` refuses every caller until the operator sets `CONTINUUM_MCP_CONFIRM_TOKEN`, because an agent allowed to record progress must not also be able to confirm it. The default path stays human-driven: run `continuum confirm <run_id>` on the host.
- **Unbuilt components**: Cloud API (Phase 13).
- **Shell command enforcement gap**: the gate enforces claims for structured tool calls but cannot see inside Bash/curl commands. Documented as v1 scope refusal.
- **Framework adapters remain experimental.** All three framework adapters now carry live-model soft-resume and hard-crash proofs (OpenRouter, `gpt-4o-mini`), including the crash contract that blocks resume on an uncertain side effect, and now have crash-and-resume verification tests achieving parity with the generic facade (Refs #285). Prefer `GenericAgentAdapter` for production recovery.
- **Agent/MCP runs need an explicit confirm before auto-resume.** Externally-reported state is `REQUIRES_REVIEW`, so `continuum resume` returns `request_human` until a human confirms. By design, not a bug; see [Framework Integration](#framework-integration).
- **e2e autonomy test series** (issue [#6](https://github.com/Cyrax321/CONTINUUM/issues/6)): three full Claude Code runs scored 7/7 mechanics with unprompted recovery behavior observed. Further iterations across diverse prompt styles remain open.

## Contributing

Contributions are welcome. This project is open source under Apache 2.0 and deliberately built to be extended: by researchers validating the recovery semantics, by engineers porting the ledger or MCP server to other frameworks or languages, and by anyone turning the planned roadmap into reality. A good place to start is the `good first issue` label on the [issue tracker](https://github.com/Cyrax321/CONTINUUM/issues), or the open correctness bugs listed in STATUS.md.

Open an issue before submitting large PRs. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guide, including the [Code of Conduct](CODE_OF_CONDUCT.md).

### Contributors

<a href="https://github.com/Cyrax321"><img src="docs/contributors/cyrax321.png" width="60" alt="Cyrax321" /></a>
  <a href="https://github.com/dchaudhari7177"><img src="docs/contributors/dchaudhari7177.png" width="60" alt="Dipak Chaudhari" /></a>
  <a href="https://github.com/lesbass"><img src="docs/contributors/lesbass.png" width="60" alt="Stefano Maffeis" /></a>
  <a href="https://github.com/as950118"><img src="docs/contributors/as950118.png" width="60" alt="heonjinjeong" /></a>
  <a href="https://github.com/abyyxhek"><img src="docs/contributors/abyyxhek.png" width="60" alt="Abishek" /></a>
  <a href="https://github.com/Parthipashok04"><img src="docs/contributors/parthipashok04.png" width="60" alt="Parthipashok04" /></a>

Also with merged contributions: [Adhi1-2](https://github.com/Adhi1-2), [yuki-fuyutsuki](https://github.com/yuki-fuyutsuki), and [okestroHjJeong](https://github.com/okestroHjJeong).

## Sponsor

If CONTINUUM helps your agents recover reliably, consider sponsoring to support long term maintenance.

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321"><img src="https://img.shields.io/badge/Sponsor-❤-ff69b4?style=for-the-badge&logo=githubsponsors" alt="Sponsor Cyrax321" /></a>
</p>

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321">Become a sponsor</a> — GitHub Sponsors, or add FUNDING.yml custom link if you prefer another platform.
</p>

## License

Apache 2.0 - see [LICENSE](LICENSE).

---

Deep reference material:

- [references/install.md](references/install.md) - prerequisites, install levels, package map, verification
- [references/concepts.md](references/concepts.md) - semantic checkpoints, validation, ledger, recovery modes, contract
- [references/architecture.md](references/architecture.md) - data model, event log, projection, storage, checkpointing, recovery engine, security
- [references/adapters.md](references/adapters.md) - framework adapter usage and live-model validation results
- [references/api.md](references/api.md) - Python and adapter API
- [references/cli.md](references/cli.md) - full CLI command list, exit codes, state diff
- [references/mcp.md](references/mcp.md) - MCP server status, verification, open questions
- [references/bench.md](references/bench.md) - CONTINUUM-Bench design
- [references/quickstart.md](references/quickstart.md) - install, examples, the proof scripts
- [references/e2e.md](references/e2e.md) - end to end autonomy test walkthrough
- [references/testing.md](references/testing.md) - test suite layout and conventions
- [references/related-work.md](references/related-work.md) - annotated related work and citation audit

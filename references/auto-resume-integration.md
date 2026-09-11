# CONTINUUM  -  Auto-Resume Integration: Architecture, Current State, and Open Problems

> **Purpose of this document.** CONTINUUM is a durability/recovery layer for
> long-running AI agents. This file explains (a) what the project is, (b) how it
> is built, (c) the specific integration we added so an LLM coding agent
> (Claude Code) can be interrupted and resumed automatically, and (d) the
> concrete problems with that integration today. It is written so that an
> engineer with **no prior knowledge of this repo** can understand it and propose
> solutions. Sections 6–8 are the actual ask.

---

## 1. What CONTINUUM is

CONTINUUM gives a long-running agent a **durable, tamper-evident recovery layer**.
Instead of an agent holding its state in memory (lost on crash), CONTINUUM records
everything as an append-only event log and derives durable state from it. Key
properties:

- **Event-sourced.** All facts are `Event`s appended to a per-run log with a
  monotonically increasing sequence and a chain hash. History is immutable; state
  is *projected* from it.
- **Checkpoints.** Semantic state is periodically snapshotted.
- **Validation.** A run can be re-validated against the live environment to
  detect drift (e.g. a dataset the run depended on changed underneath it).
- **Action ledger.** Side effects (deploy, send, write) are recorded idempotently
  so a resumed agent never repeats an effect it already performed.
- **Recovery engine.** Decides *how* (and *whether*) a run may resume after an
  interruption: `RESUME`, `REPAIR_AND_RESUME`, or `REQUEST_HUMAN`.
- **Adapters + MCP + CLI.** The same core is exposed three ways: framework
  adapters (LangGraph/LangChain/OpenAI/Generic), an MCP server (`continuum-mcp`)
  for any MCP client, and a `continuum` command-line tool.

Storage backends include SQLite (the default used here).

---

## 2. Core architecture (modules)

| Module | Responsibility |
|--------|----------------|
| `continuum.events` | `EventType` enum, `Event`, append/sequence/chain-hash machinery. |
| `continuum.models` | `Run`, `RunStatus`, `Progress`, `SemanticState`, `StateStatus`, `ActionStatus`, `RecoveryMode`, `Contract`. |
| `continuum.storage` (`base.py`, `sqlite.py`) | `Storage` interface + `SQLiteStorage`: `create_run`, `get_run`, `list_runs`, `get_active_run`, `append_event`, checkpoints, actions. |
| `continuum.state.semantic` | `project(events) -> SemanticState`; folds the event log into current state. |
| `continuum.checkpoint` | `CheckpointManager.restore()` / checkpoint policy. |
| `continuum.actions.ledger` | `ActionLedger`, `ActionOutcome`, idempotency/dedup logic. |
| `continuum.environment` | `capture()`, `StaticProvider`, `EnvironmentSnapshot`  -  what the run depended on. |
| `continuum.recovery` (`engine.py`, `planner.py`, `contract.py`) | `RecoveryEngine.assess()` computes a `RecoveryDecision` (mode, contract, repair plan). |
| `continuum.adapters` | `AgentAdapter` base + `GenericAgentAdapter`, `LangGraphAgentAdapter`, `LangChainAgentAdapter`, `OpenAIAgentAdapter`. Wrap an agent loop so checkpoint/interception/resume happen automatically. `resume()` delegates to `RecoveryEngine`. |
| `continuum.mcp` (`server.py`, `authz.py`) | `build_server()` builds the MCP server + `ContinuumMCP` context; `authz.py` is caller auth + authorization. |
| `continuum.security.attestation` | Cryptographic signing/verification of a run's event chain. |
| `continuum.cli.main` | The `continuum` command (init, runs, inspect, history, events, diff, validate, resume, confirm, checkpoint, verify, actions, show-contract, replay, benchmark, attest*, serve). |
| `continuum.benchmark` | `run_benchmark`, `run_idempotency_benchmark`, `SCENARIOS`, `METHODS` (controlled-failure benchmarks). |

### Run lifecycle (`RunStatus`)

`PLANNED, STARTED, RUNNING, CHECKPOINTED, SUSPENDED, COMPLETED, CRASHED, ABORTED, FAILED`.

A run *interrupted* by killing the terminal is left in a **non-terminal** state
(`STARTED`/`RUNNING`/`CHECKPOINTED`/…). It is **not** automatically marked
`CRASHED`, because nothing runs to mark it. That non-terminal status is exactly
what lets it be "found" for resume.

---

## 3. The MCP server (`continuum-mcp`)

Registered in `.mcp.json` (project root):

```json
{
  "mcpServers": {
    "continuum-mcp": {
      "command": "continuum-mcp",
      "args": ["--db", "continuum.db"],
      "env": { "CONTINUUM_MCP_MUTATING_CLIENTS": "claude-code" }
    }
  }
}
```

- Spawns as a stdio MCP server; it opens `continuum.db` in the current working
  directory.
- **Twelve tools**, split read-only vs mutating:
  `continuum_record_progress`, `continuum_checkpoint`,
  `continuum_record_summary`, `continuum_intercept_action`,
  `continuum_complete_action`, `continuum_fail_action`,
  `continuum_reconcile_action`, `continuum_confirm`
  (mutating), and `continuum_validate`, `continuum_resume`,
  `continuum_list_actions` (read-only).
- **Authorization** (`authz.py`): mutating tools are gated by an allowlist
  (`CONTINUUM_MCP_MUTATING_CLIENTS`, here `claude-code`). A caller whose
  declared `clientInfo.name` is not on the list is refused *before* any write.
- **Authentication** (`authz.py`): optional shared secret
  (`CONTINUUM_MCP_TOKEN`) or per-client tokens (`CONTINUUM_MCP_CLIENT_TOKENS`);
  disabled when unset (the local, no-account case).

`continuum_resume` is the key tool for this integration. Its current signature:

```python
def continuum_resume(run_id: str | None = None,
                     env: dict[str, str] | None = None,
                     expected_model: str | None = None) -> str:
```

- If `run_id` is omitted it calls `Storage.get_active_run()` and targets that.
- Returns a JSON blob including: `run_id`, `goal` (the run's stored task
  string), `mode` (`resume` / `request_human` / `repair` / `no_active_run`),
  `safe`, `progress` (`completed`/`pending`/`total`/`failed`), `contract`,
  `repairs`, `uncertain_actions`, `report`.

`Storage.get_active_run()` (SQLite):

```python
def get_active_run(self) -> Run | None:
    terminal = (RunStatus.COMPLETED.value, RunStatus.CRASHED.value,
                RunStatus.ABORTED.value, RunStatus.FAILED.value)
    rows = conn.execute(
        "SELECT * FROM runs WHERE status NOT IN (?,?,?,?) "
        "ORDER BY updated_at DESC, run_id DESC LIMIT 1", terminal)
    return self._row_to_run(rows[0]) if rows else None
```

`continuum_record_progress` is how a run is *created*: it appends
`RUN_STARTED` + `TASK_UPDATED` and stores the task in `Run.goal`. So the very
first progress call both starts the run and persists the task as `goal`.

---

## 4. The intended UX (the feature we are building)

The goal: an agent works on a long task; CONTINUUM records every step; the user
**kills the terminal mid-task**; the user **opens a new terminal and launches the
agent again**; the agent **automatically detects the interrupted run and offers
to continue**, and on "yes" resumes exactly where it left off  -  all via the MCP
server, with no memorized run id and no manual setup.

Concretely:

1. User launches Claude Code in the project dir. It loads `CLAUDE.md` (project
   instructions) and connects to `continuum-mcp` (from `.mcp.json`, enabled in
   `.claude/settings.local.json`).
2. User gives a task. Per `CLAUDE.md`, the agent's **first action** is
   `continuum_resume` (no `run_id`).
3. If none: agent starts a fresh run via `continuum_record_progress(run_id,
   completed=0, total=N, goal="<task>")`, then works, calling
   `continuum_checkpoint(run_id)` after each unit.
4. User kills the terminal. The run stays in a non-terminal state.
5. User opens a new terminal, launches Claude Code again, sends any message.
   The agent again calls `continuum_resume` (no id) → finds the active run →
   shows `goal` + progress → **asks the user**: *"Resume it, or start a new
   task?"*
6. User says resume → agent continues from the recorded progress.

This is wired today via `CLAUDE.md` (the "glue"), not via code changes to
Claude Code itself.

---

## 5. Current `CLAUDE.md` (the integration glue)

```markdown
# CONTINUUM durable agent
... (project ships continuum-mcp in .mcp.json) ...

## On session start, detect and ask
Your first action is to call continuum_resume with no run_id.
- If an in-progress run is returned: show run_id, progress, goal, then ASK the
  user: "I found an unfinished task in CONTINUUM  -  run <id> at <c>/<t>: '<goal>'.
  Resume it, or start a new task?" Then wait.
  - resume: if mode == request_human, call continuum_confirm(run_id) first, then
    continue from the recorded progress.
  - new: start a fresh run.
- If no_active_run: just do what the user asked.

Do not read or write any task file  -  the task is the run's goal, which
continuum_resume returns, so a resumed session already knows what to continue.

## While working, record every step (cheap, no extra files)
After each meaningful unit of work, call:
- continuum_record_progress(run_id, completed, total, goal="<the task>")
- continuum_checkpoint(run_id)
```

---

## 6. The problems (why this is "a big issue")

The integration is **functionally correct** (it does detect and resume) but is
**slow and token-expensive in practice**, and the task content often does not
start for a long time. Concretely observed during testing:

### P1. Heavy pre-work before the first unit of real output
Before writing anything, the agent performs several tool round-trips:
`continuum_resume` (stdio MCP call), and  -  in earlier versions  -  a `Write` of a
`CONTINUUM_TASK.md` file plus extensive codebase exploration (`ls`/`find`/`grep`
of `src/`, `docs/`, `authz.py`) to make generated snippets "accurate". Each
tool call is request+response tokens, and the exploration happened *before any
section was written*. We mitigated this by (a) removing `CONTINUUM_TASK.md` and
storing the task as the run `goal` (resume now returns it), (b) slimming
`CLAUDE.md` to detect-and-ask + checkpoint only, and (c) using self-contained
tasks. **This helped but did not eliminate the underlying cost** (see P2–P5).

### P2. "Detect in milliseconds" is not actually achievable with agent reasoning
A Claude Code session only *acts* after the user sends a message. The detection
is one tool call, but the latency is: user message → model inference → tool call
→ MCP stdio round-trip → model formats the question → user reads it. That is
seconds, and requires the user to type something first. There is currently **no
autonomous session-start trigger**  -  `CLAUDE.md` only fires *after* the first
user message. True "open the terminal and it instantly asks" would need a
`SessionStart` hook or a wrapper script that runs `continuum resume` and prints a
prompt, not agent reasoning.

### P3. The self-certified → request_human → confirm dance
A run created purely via MCP tools is **self-certified** (its goal/progress were
reported by the agent, not a human). On resume, `continuum_resume` therefore
returns `mode: request_human` (not `resume`) until a `REVIEW_CONFIRMED` event
exists. So every resume currently *requires* an extra `continuum_confirm`
tool call + an extra model turn before the agent may continue. This is a
mandatory per-resume tax on latency and tokens.

### P4. Task/context recovery is lossy
On resume, the agent reconstructs "what to do next" from the `goal` string (free
text) plus the `completed`/`total` counters. For anything beyond a simple
sequential list ("write sections 1..5"), this is insufficient, and agents tend
to **re-explore the repo or re-ask clarifying questions** to recover context  - 
reintroducing the P1 overhead on the *resume* side too.

### P5. MCP cold-start fragility
On launch we observed `MCP Wait For Servers: ready: false  -  Unknown (no MCP
server with this name is configured): continuum-mcp`, requiring a manual `/mcp`
reconnect before the agent could use the tools. This adds friction and a
per-session delay, and would break an unattended "instant resume."

### P6. Checkpoint granularity depends on LLM discipline
Whether a unit of work is actually checkpointed is entirely up to the model
choosing to call `continuum_checkpoint`. If it batches several units before
checkpointing, an interruption loses that granularity and the resume point is
coarser than desired.

### P7. Per-session token floor
Every session pays for the system prompt (`CLAUDE.md`) plus the schemas of all
twelve MCP tools, regardless of how little work is done. For many short
resume checks this fixed cost dominates.

---

## 7. What we have already tried (do not re-litigate)

- Added `Storage.get_active_run()` + made `continuum_resume` accept an optional
  `run_id` defaulting to it. (So a fresh session needs no memorized id.)
- Added `goal` to the `continuum_resume` response so resume is self-describing.
- Removed the `CONTINUUM_TASK.md` file writes; task now lives in `Run.goal`.
- Slimmed `CLAUDE.md` to detect-and-ask + checkpoint only.
- Confirmed `claude-code` is permitted by the MCP authorization allowlist.
- Verified the resume-with-no-id path with unit tests.

None of these changed the *fundamental* cost structure in P2–P7; they only
reduced the *avoidable* exploration/file overhead.

---

## 8. What we need help solving

Please propose concrete designs (with code-level changes where applicable) for
any of the following. Priorities are P2/P3/P4 (latency + context fidelity) and
P5 (robustness).

1. **Instant, autonomous detection.** How can a fresh Claude Code session detect
   and surface the interrupted run *without* waiting for a user message and
   without a full model-inference round-trip? (e.g. `SessionStart` hook that runs
   `continuum resume` and injects a pre-rendered "resume or new?" prompt; a tiny
   wrapper script; a precomputed resume banner.) What is the cleanest mechanism
   given Claude Code's extensibility?

2. **Eliminate the confirm tax (P3).** The self-certified → `request_human` →
   `continuum_confirm` dance adds a mandatory tool call + turn every resume.
   Options to consider: auto-confirm self-certified runs that are being resumed
   by the same client; a trust model where the agent *is* the operator; a
   different resume mode that does not require `REVIEW_CONFIRMED` for
   agent-originated runs. What is safe and minimal?

3. **Durable, lossless task context (P4).** Storing the task as a free-text
   `goal` is fragile. Should CONTINUUM store a structured plan/checklist artifact
   (per-unit status) so a resumed session knows exactly which units remain
   without re-exploring? Where would that live (a new event type? a plan table?)
   and how would the agent maintain it cheaply?

4. **Checkpoint guarantee independent of LLM discipline (P6).** Can checkpoints
   be captured automatically (e.g. a hook that calls `continuum_checkpoint` after
   each assistant turn / file write) so granularity no longer depends on the
   model remembering to call the tool?

5. **MCP cold-start robustness (P5).** Why does `continuum-mcp` sometimes report
   `ready: false` at session start, and how should it be configured (or the
   server hardened) so it is reliably connected before the first tool call?

6. **Token floor (P7).** Is there a way to reduce the per-session cost of the
   MCP tool surface / system prompt for what is essentially a "resume check"?

---

## 9. How to reproduce the current behavior

```bash
cd <project root>
claude                                   # interactive Claude Code
# paste a self-contained task, e.g.:
#   Write a 5-part beginner's guide to Git version control. Sections:
#   (1) What Git is, (2) The basic workflow (init/add/commit), (3) Branches,
#   (4) Merging and conflicts, (5) Remotes and collaboration. ~150 words each,
#   with one command example per part.
# agent calls continuum_resume -> no_active_run -> starts run "guide",
# writes + checkpoints each section
# ... kill the terminal after 2-3 sections ...
claude                                   # new terminal
# type: hi
# agent calls continuum_resume -> finds "guide" at N/5, asks resume/new
```

Verify:
```bash
continuum --db continuum.db resume           # shows mode/goal/progress
continuum --db continuum.db events guide     # the recorded section trail
```

## 10. Key files to read

- `src/continuum/mcp/server.py`  -  `build_server`, `ContinuumMCP`, the 12 tools,
  `continuum_resume` (optional `run_id`, returns `goal`).
- `src/continuum/storage/sqlite.py`  -  `get_active_run`, `list_runs`, run table.
- `src/continuum/recovery/engine.py`  -  `RecoveryEngine.assess` (mode decision).
- `src/continuum/adapters/generic.py`  -  `GenericAgentAdapter.resume`.
- `src/continuum/mcp/authz.py`  -  `AuthorizationPolicy`, `AuthPolicy`, `load_auth`.
- `src/continuum/cli/main.py`  -  `cmd_resume` (also returns `goal` now).
- `.mcp.json`, `.claude/settings.local.json`, `CLAUDE.md`  -  the integration glue.
- `src/continuum/models.py`  -  `Run`, `RunStatus`, `RecoveryMode`.

# Project status

**As of 2026-08-14** (commit `a539948`). On 2026-08-14 a repository-wide bug
audit ran: every behavioural module was read and exercised, surfacing 20
evidence-backed issues (#29-"#49, excluding the externally-filed #39). They are
labelled `good first issue` or `help wanted` (plus `adapter`/`detector` where
relevant) and form the contributor backlog; none are fixed in this tree yet.

A factual snapshot for whoever picks this up next, human or otherwise, with no
memory of how any of it was found. It records what is verified, what is
believed, and what is neither.

---

## Verified

677 tests pass, 4 skipped, on Python 3.13 with `mcp 2.0.0` installed. The MCP
server tests are no longer excluded: they load and pass against `mcp>=2.0` (the
version pinned in `pyproject.toml`). An earlier note recorded them as failing to
load; that incompatibility is gone with the newer SDK. CI was green on Python
3.11, 3.12 and 3.13, plus lint (`ruff`) and strict type-check (`mypy`), confirmed
by run
[31355087372](https://github.com/Cyrax321/CONTINUUM/actions/runs/31355087372).
Everything below has tests behind it; several of the safety-critical paths have
also been checked by deliberately breaking them and confirming the suite
notices.

### Core

| Component | Module | Notes |
|:--|:--|:--|
| Event log | `events.py` | Append-only, hash-chained, per-run sequencing. `verify()` reports `trusted_through` so a partially tampered run can still be recovered up to its last good event. |
| State projection | `state/semantic.py` | Pure fold over an event prefix. Reproducible and prefix-closed. |
| Storage | `storage/sqlite.py` | WAL, `synchronous=FULL`, `IMMEDIATE` write transactions, `UNIQUE(run_id, sequence)`. Schema **v2**. |
| Checkpoints | `checkpoint/` | Policy-driven (manual, interval, event, semantic, context-pressure, hybrid). Restore replays events recorded after the checkpoint. |
| Validation | `state/validator.py` | Checks state against the current environment. Staleness propagates `dependency -> evidence -> finding -> decision`. |
| Action ledger | `actions/` | Idempotent claim/complete. Raises `UnknownSideEffect` rather than guessing when an outcome is unknown. |
| Recovery engine | `recovery/` | Reduces validation, ledger and checkpoint signals to one `RecoveryMode`. Takes the **maximum** on a severity ordering, so the most cautious signal wins regardless of evaluation order. |

### Interfaces

- **CLI** (`cli/`) — 14 commands, stdlib `argparse` only. Exit codes are a
  safety contract: only a verified-safe run exits `0`, so
  `continuum resume "$RUN" && ./start-agent.sh` cannot launch onto stale state.
  Colour is TTY-aware and respects `NO_COLOR`; piped output is byte-identical
  to uncoloured output.
- **`GenericAgentAdapter`** (`adapters/generic.py`) — in-process Python facade.
- **`LangGraphAgentAdapter`** (`adapters/langgraph.py`) — LangGraph
  integration, optional `langgraph` dependency.
- **`OpenAIAgentAdapter`** (`adapters/openai.py`) — OpenAI Agents SDK
  integration, optional `openai-agents` dependency.
- **MCP server** (`mcp/server.py`) — 9 tools over stdio.

### MCP two-phase action interception

A Python callable cannot cross the MCP boundary, so the server cannot execute a
side effect on the caller's behalf. The protocol is:

1. `continuum_intercept_action` — claims the ledger entry, answers *may I?*
2. the caller performs the effect
3. `continuum_complete_action` — records the outcome

Between 1 and 3 the ledger holds a `STARTED` record. A caller that crashes or
never reports back leaves the action uncertain, and recovery refuses to resume
until it is reconciled. **This is intended.** An unreported effect is
indistinguishable from a completed one, and surfacing that is the ledger's
entire purpose.

---

## The self-certification fix (`9738b9e`)

### What the exploit was

An agent reaching the MCP server could fabricate progress and have CONTINUUM
confirm it was safe to resume:

```
record_progress(run_id='r', completed=9999, total=10000, goal=...)
checkpoint(run_id='r')
resume(run_id='r')

before:  mode=resume         safe=True
after:   mode=request_human  safe=False
```

### Why it happened

The projector hardcoded `Origin.DETERMINISTIC` for every component it folded.
That was true of the *fold* and false of the *claim* — faithfully folding a
fabricated event yields a faithful projection of a lie. `Origin` and
`Provenance` already existed, but neither the validator nor the recovery engine
consulted them, and `Goal`/`Progress` — the two fields the exploit falsifies —
carried no provenance at all.

### What closed it

- `Event.source` records who asserted a fact, captured at write time and
  included in `content()` so it is **signed**. A trust marker outside the digest
  could be edited without breaking verification.
- The projector propagates `event.source` instead of hardcoding. `Goal` and
  `Progress` now carry provenance.
- The validator marks self-certified components `REQUIRES_REVIEW`.
- Everything written through MCP is tagged `Origin.EXTERNAL_AGENT`.

Progress is cumulative, so the weakest contributor wins: a trusted event
appended after an agent's self-report does not launder the running total.

This required a **strict schema migration, v1 to v2, with no compatibility
branch**. Pre-existing event chains no longer verify. The database was reset as
part of the change.

Commit: `9738b9e`.

### What it does *not* fix

Provenance stops an agent certifying its own state. It does **not** stop an
unauthorized caller invoking mutating tools in the first place — that is the
authorization layer below.

---

## The MCP authorization layer (`d9365c8`)

Any client that could reach the server could call any tool. Several agents have
been configured against this project's database simultaneously — Kilo, Gemini
CLI and Claude Code all pointed at the same `continuum.db` — so any of them
could overwrite another's progress, checkpoint over its state, or claim its
actions.

Mutating tools now require the caller to appear on an allowlist. The caller is
identified by `client_info.name` from the initialize handshake, which the
transport injects server-side; a caller cannot elevate itself by passing a
forged `clientInfo` in tool arguments (verified against the live stdio
transport and covered by test).

**Deny by default.** An unlisted caller is not one we have decided to trust; it
is one nobody has made a decision about. An unconfigured server is therefore
read-only. This matches the validator's stance elsewhere: uncertainty degrades
rather than resolving in its own favour.

Policy resolves in four tiers, each replacing rather than merging the ones
below, so `AuthorizationPolicy.source` always names where a grant came from:

1. explicit `policy=` argument
2. `CONTINUUM_MCP_MUTATING_CLIENTS`, or its alias `CONTINUUM_MCP_ALLOW`
3. `.continuum/mcp-policy.json`
4. deny

A malformed policy file raises rather than falling back — a file that exists is
a statement of intent, and ignoring a typo in it would either baffle the owner
or quietly widen access.

**Read-only tools are not gated.** `validate`, `resume` and `list_actions`
cannot alter a run, and their value is that anyone can ask "is this safe to
continue?" without first being granted permission. Gating them would also leave
an unlisted caller unable to discover why its writes are failing. The
information they disclose is already readable by anyone holding the database
file. The split is driven by the `read_only_hint` annotation each tool already
declared, so the two cannot drift apart.

### What it does *not* do

`clientInfo` is asserted by the client at handshake and never verified. A caller
that wants to be seen as `claude-code` simply says so. **This is authorization
by declared identity, not authentication.**

It keeps honestly-named coexisting agents out of each other's runs. It does not
defend against a deliberately impersonating or malicious local process — which
in any case has direct filesystem access to the database and does not need the
MCP server at all.

Also out of scope: rate limiting, audit of failed attempts beyond the error
response, and scoping callers to particular runs.

### Naming (`a539948`)

`CONTINUUM_MCP_MUTATING_CLIENTS` is accepted as an alias for
`CONTINUUM_MCP_ALLOW`, occupying the same precedence position rather than adding
a config source. If both are set the longer name wins, since it states what is
being allowed; `policy.source` reports which was used. The name is preserved
from the closed PR #3 below.

---

## PR #3: an authorization attempt that failed open

Worth reading before touching this code.
[PR #3](https://github.com/Cyrax321/CONTINUUM/pull/3) was an independently
developed attempt at the same fix, with the right shape — handshake identity,
enforcement at the MCP boundary, read-only tools left callable. It was reviewed
and **closed without merging** because its guard authorized the caller on two
failure paths:

```python
if mcp_context is None:
    return                    # allows
try:
    params = mcp_context.session.client_params
except ValueError:
    return                    # allows
```

The `ValueError` path is reachable: `Context.request_context` raises exactly
`ValueError("Context is not available outside of a request")` when `.session` is
touched outside a live request. Replicating the guard verbatim and invoking it
the way the test suite invokes tools produced
`{"authorized": true, "why": "ValueError -> allowed"}`.

The instructive part is not the missing `raise`. The PR modified
`tests/test_mcp_server.py` but added no test of the gate itself, so the fail-open
produced a green checkmark — and its `Fixes #1` footer would have auto-closed
the issue on merge. Passing tests, a closed issue, and an open hole is a worse
outcome than no fix at all.

Two things were kept from it: the `CONTINUUM_MCP_MUTATING_CLIENTS` name, and the
observation that raising `ToolError` directly is a defensible alternative to the
`PermissionError` subclass used here.

---

## Open items

| Issue | Summary | Priority |
|:--|:--|:--|
| [#1](https://github.com/Cyrax321/CONTINUUM/issues/1) | **MCP caller authentication.** Narrowed by `d9365c8`: authorization for mutating tools now exists and denies by default. What remains is authentication — `clientInfo` is client-asserted and unverified, so a deliberately impersonating local process is unaffected. Would need a shared secret, per-client token, or transport-level identity. | Medium |

### Code audit findings (2026-08-12)

A module-by-module audit filed seven issues, each reproduced against clean
`HEAD` (455e307) and filed with the `bug_report` template:

| Issue | Summary | Priority | Status |
|:--|:--|:--|:--|
| [#15](https://github.com/Cyrax321/CONTINUUM/issues/15) | **Over-total progress is a partial write.** `record_progress`/event writers commit a `TASK_UPDATED` whose `completed + pending + failed > total`; the log then passes `verify_events` but every projection, checkpoint, resume and validate raises a raw pydantic `ValidationError`, permanently, with no rollback. | High | Resolved — `91aee41` rejects over-total progress before it is written, raising `ToolError`/`ValidationError` at the boundary rather than committing a corruptible event. |
| [#20](https://github.com/Cyrax321/CONTINUUM/issues/20) | **Read-only `list_actions` writes.** Annotated `read_only` (and therefore ungated), `continuum_list_actions` calls `ensure_run`, backfilling `RUN_STARTED` into a bare run's log. Contradicts the read-only split guarantee. | High | Resolved — `71c86b3` resolves the run via `get_run` instead of `ensure_run`, so a bare run lists zero actions without appending anything. |
| [#16](https://github.com/Cyrax321/CONTINUUM/issues/16) | **STALE STATE section droppable.** `build_recovery_context` protects sections by sorted index, not identity: with `next_action` present, the STALE STATE section falls outside the `protected = 3` window and is dropped under a tight budget despite the never-dropped promise. | High | Resolved — `e9c5f78` protects the never-dropped sections (`CURRENT GOAL`, `VERIFIED PROGRESS`, `STALE STATE — DO NOT RELY ON`) by identity via a `_NEVER_DROPPED` set, so an injected higher-priority section cannot push stale state out of the protected set. |
| [#21](https://github.com/Cyrax321/CONTINUUM/issues/21) | **OpenAI adapter cannot auto-provision runs.** `_ensure_run_exists` reads via `get_run` which raises rather than returning `None`, so its `create_run` branch is dead code and `on_agent_start` raises `RunNotFound` for any fresh run. | Medium | Resolved: `_ensure_run_exists` now catches `RunNotFound` and creates the run, so a fresh OpenAI agent run is auto-provisioned on first contact; two regression tests in `tests/test_adapters_openai.py` cover the create-on-missing and idempotent-exists paths. |
| [#17](https://github.com/Cyrax321/CONTINUUM/issues/17) | **Older-schema DB accepted silently.** A pre-v2 file opens without `SchemaVersionError` (only newer versions are rejected), `read_events` returns `[]` for a populated run, and the first write fails with a raw sqlite `OperationalError`. No migration path exists. | Medium | Resolved — `82b9f1c` raises `SchemaVersionError` at open when the stored schema version is below `SCHEMA_VERSION`; adds `tests/test_storage.py::test_an_older_schema_is_refused`. |
| [#19](https://github.com/Cyrax321/CONTINUUM/issues/19) | **`resume --repair` is a no-op.** Help and docstrings claim `--repair` records the repair plan (and is one of only three mutating commands); in practice it only suppresses a stderr hint, writing nothing. | Medium | Resolved — `f145818` makes `cmd_resume` append a `RECOVERY_STARTED` event carrying the plan steps when `--repair` is given and a plan exists; adds `tests/test_cli.py::test_repair_records_the_plan_and_does_not_fake_a_safe_exit` and `tests/test_cli.py::test_resume_without_repair_is_still_read_only`. |
| [#18](https://github.com/Cyrax321/CONTINUUM/issues/18) | **`events` breaks the exit-code contract.** `continuum events $MISSING` exits 0 with "No events.", while every other run-scoped command exits 2; `events` is absent from the enforcing parametrised test. Tagged `good first issue`. | Medium | Resolved: `1bcc933` gates `cmd_events` on `get_run` (which raises `RunNotFound`, mapped to `NOT_FOUND` by the dispatcher) and adds `events` to the missing-run parametrised test. |
| Orphaned-WAL startup crash | **MCP server fails to connect after a hard-kill.** A killed server leaves `<db>-wal`/`<db>-shm` sidecars that make `PRAGMA journal_mode=WAL` throw `disk I/O error` on the next launch. | Medium | Resolved: `_open_server_storage` in `src/continuum/mcp/server.py` clears orphaned sidecars and retries the open once on `OperationalError` (re-raising when there was nothing to clear), with two regression tests in `tests/test_mcp_server.py`. See the MCP server section. |
| Issue #6 e2e dedup defect | **`continuum_intercept_action` deduplicated on raw argument formatting, not resource identity.** Three real Claude Code e2e runs showed session 2 getting `proceed: true` for invoices session 1 already sent, because relative-path vs absolute-path arguments hashed to different idempotency keys. Correctness survived only because the agents cross-checked the outbox and refused the flag. | High | Resolved twice over: the tool accepts a stable `key` (e.g. `invoice:INV-001`) that is what makes two attempts the same action, and a defensive layer now covers the no-key and argument-drift cases (path canonicalization plus a token-based identity fallback in `ActionLedger.claim`). Regression tests: `test_a_stable_key_deduplicates_across_argument_shape_changes` plus the identity-match and canonicalization tests in `tests/test_action_ledger.py`. |
| Stale editable metadata | `pip show continuum-agent` reports its editable location as `Desktop/untitled folder 2` (the pre-move path); imports still resolve correctly, so it is cosmetic. A clean `pip install -e ".[mcp]"` from the current project root fixes it. | Low | Open |

### Launch audit (2026-08-14)

A second module-by-module audit filed 20 issues (#29-#49, minus external #39).
The three launch-critical defects were fixed and closed in `e8271bd`:

- **#35** — MCP self-certified progress: added `REVIEW_CONFIRMED` event,
  `continuum confirm` CLI, and `continuum_confirm` MCP tool; `StateValidator`
  and `RecoveryEngine.assess` clear self-certified `REQUIRES_REVIEW` once a
  confirmation event exists.
- **#46** — LangGraph synthetic state: `checkpoint_node` now projects real
  state from the event log when events exist, instead of emitting an empty dict.
- **#47** — OpenAI adapter `RUN_STARTED` backfill: `_ensure_run_exists` now
  backfills `RUN_STARTED` like `ContinuumMCP.ensure_run`.

The remaining **17 are real but non-launch-critical and are left open as
contributor work** (labeled `good first issue` / `help wanted`):

#29, #30, #31, #32, #33, #34, #36, #37, #38, #40, #41, #42, #43, #44, #45, #48, #49.

#### Known issues at launch

| Issue | One-line impact | Disposition |
|:--|:--|:--|
| [#29](https://github.com/Cyrax321/CONTINUUM/issues/29) | `ActionLedger.reconcile(occurred=False)` leaves stale `external_id`/`result` on the action | Open (contributor work) |
| [#30](https://github.com/Cyrax321/CONTINUUM/issues/30) | `FileProvider` reports a missing file as `version=None`, so diff marks it `changed` not `removed` | Open (contributor work) |
| [#31](https://github.com/Cyrax321/CONTINUUM/issues/31) | `continuum replay` claims to confirm state matches the stored version but never compares | Open (contributor work) |
| [#32](https://github.com/Cyrax321/CONTINUUM/issues/32) | `continuum replay --upto N` crashes with `ProjectionError` when the prefix excludes `RUN_STARTED` | Open (contributor work) |
| [#33](https://github.com/Cyrax321/CONTINUUM/issues/33) | `identity_tokens` drops plain-word resource ids (`invoice`) because `_is_strong_token` requires a digit/`@`/`.` | Open (contributor work) |
| [#34](https://github.com/Cyrax321/CONTINUUM/issues/34) | `ActionLedger scoped_to_run=False` does not enforce global uniqueness across runs as documented | Open (contributor work) |
| [#36](https://github.com/Cyrax321/CONTINUUM/issues/36) | `identity_tokens` drops purely-numeric resource ids, so cross-session fallback fails on numeric ids | Open (contributor work) |
| [#37](https://github.com/Cyrax321/CONTINUUM/issues/37) | OpenAI adapter: tool arguments misbound and idempotency bypassed because `__signature__` drops `ctx` | Open (contributor work) |
| [#38](https://github.com/Cyrax321/CONTINUUM/issues/38) | `continuum_record_progress` accepts negative `completed`/`failed` when `total` is omitted, poisoning the event log | Open (contributor work) |
| [#40](https://github.com/Cyrax321/CONTINUUM/issues/40) | `LLMExtractor`: malformed LLM proposal crashes `extract()` instead of falling back | Open (contributor work) |
| [#41](https://github.com/Cyrax321/CONTINUUM/issues/41) | `LLMExtractor._merge` double-adds duplicate ids within a single proposal | Open (contributor work) |
| [#42](https://github.com/Cyrax321/CONTINUUM/issues/42) | Strict mode: uncertain side effect yields `REQUEST_HUMAN` but an auto-reconcile step silently ignores `strict_unknown` | Open (contributor work) |
| [#43](https://github.com/Cyrax321/CONTINUUM/issues/43) | Two checkpoints at the same state version collapse to one in `continuum history` | Open (contributor work) |
| [#44](https://github.com/Cyrax321/CONTINUUM/issues/44) | `intercept_action` returns a divergent value on cache hit when the result dict holds reserved key `__return_value__` | Open (contributor work) |
| [#45](https://github.com/Cyrax321/CONTINUUM/issues/45) | `claim(on_unknown=)` resolution is not persisted, so the ledger stays uncertain after call-time resolution | Open (contributor work) |
| [#48](https://github.com/Cyrax321/CONTINUUM/issues/48) | `StateValidator._check_progress` relabels self-certified progress as `UNKNOWN`, so `--tolerate-unknown` silently unblocks it | Open (contributor work) |
| [#49](https://github.com/Cyrax321/CONTINUUM/issues/49) | `StateValidator._check_model` reports model-specific assumptions `VALID` when `expected_model` is `None` (fail-open) | Open (contributor work) |

None of these block the v0.1.0 launch; they are tracked for post-launch
contributor work.

## The CI Node 24 migration (2026-08-12)

### What the issue was

GitHub-hosted actions running on Node 20 were being migrated to Node 24. Actions
whose `action.yml` declares `using: node20` emit deprecation warnings and will
hard-fail once GitHub ends its grace period. Three of the actions pinned in this
project's workflows ran on Node 20:

| Action | Pin | `using` |
|:--|:--|:--|
| `actions/checkout` | v4 | `node20` |
| `actions/setup-python` | v5 | `node20` |
| `codecov/codecov-action` | v4 | `node20` |

`release.yml` had the same `checkout`/`setup-python` pins plus `upload-artifact`
and `download-artifact` at v4 (also `node20`), and `softprops/action-gh-release@v2`
(`node20`). `deploy-pages.yml` had `checkout@v4` and `deploy-pages@v4`
(`node20`).

### What closed it

Each action was bumped to the latest stable major version, which publishes
`using: node24` in its `action.yml`:

| Action | Old pin | New pin |
|:--|:--|:--|
| `actions/checkout` | v4 | **v7.0.1** |
| `actions/setup-python` | v5 | **v7.0.0** |
| `codecov/codecov-action` | v4 | **v7.0.0** |
| `actions/upload-artifact` | v4 | **v7.0.1** |
| `actions/download-artifact` | v4 | **v8.0.1** |
| `actions/configure-pages` | v5 | **v6.0.0** |
| `actions/deploy-pages` | v4 | **v5.0.0** |
| `softprops/action-gh-release` | v2 | **v3.0.2** |

`actions/upload-pages-artifact@v3` was left at v3: it uses `runs: using: composite`,
which is not subject to the Node deprecation (composite actions run as workflow
steps, not in a Node runtime). `pypa/gh-action-pypi-publish@release/v1` was also
left unchanged: it is a composite action.

### How verified

YAML syntax validated with `yaml.safe_load_all()` on all three workflow files.
The new versions were confirmed by fetching each action's latest stable release
tag via the GitHub API and reading its `action.yml` `runs.using` field to verify
`node24` (or `composite` for `upload-pages-artifact`). Confirmed by CI run
[31534363260](https://github.com/Cyrax321/CONTINUUM/actions/runs/31534363260)
— all four jobs green: ruff lint, ruff format, mypy strict, and tests on
Python 3.11 / 3.12 / 3.13.

### Not done

No action version was bumped to a prerelease, draft, or non-semver tag. All
selected versions are the highest stable semver release for each action as of
2026-08-12.

---

## Not built

Phases 13–14 of the original plan: cloud API, dashboard. The minimal
CONTINUUM-Bench harness (Phase 12) now ships: `continuum benchmark` runs and
prints measured numbers across three scenarios and three strategies. The fuller
suite, published baselines, and a dashboard view remain a Phase 12 goal.

### Framework adapters (Phase 11)

`adapters/` now contains `base.py`, `generic.py`, `langgraph.py`, and
`openai.py`. Both are optional dependencies — `langgraph` and `openai-agents`
are not pulled in by `pip install continuum-agent`; install via
`pip install continuum-agent[langgraph]` or `[openai]`. Each was written after
checking the target framework's actual API surface (ToolContext/RunHooks for
OpenAI Agents SDK; StateGraph/TypedDict for LangGraph), not an assumed shape.
Tests cover behavior without the SDK installed (mocked), with it installed
(integration class, skip-guarded), and the established `AgentAdapter`
contract.

---

## Third-party MCP client testing

Both clients connected to the server and successfully invoked tools. **Neither
completed a full checkpoint → resume cycle.**

| Client | Connected | Tools called | Outcome |
|:--|:--|:--|:--|
| Gemini CLI | yes, health-checked `✓ Connected` | `record_progress` ×4 | First call registered a run and wrote events. A later call failed on the `pending`-recomputation bug (fixed in `9738b9e`). |
| Kilo Code | yes, via its own `kilo.jsonc` | `record_progress` | Wrote a run row and a `RUN_STARTED` event, then stopped. |

Neither called `continuum_checkpoint` or `continuum_resume` at any point, even
before hitting errors. Whether that reflects tool descriptions that do not
motivate use, or simply an incomplete task, is **not established**. Worth
re-running now the blocking bug is fixed.

Note: the evidence for the Kilo run was in a database that has since been reset
by the v2 migration. The Gemini session transcript persists under
`~/.gemini/tmp/`, but that is outside the repository and not durable.

---

## MCP Inspector CLI verification (2026-08-12)

The MCP server was tested end-to-end using `@modelcontextprotocol/inspector`
v2.1.0 in `--cli` mode, which drives the real stdio protocol boundary — the
inspector spawns the server as a subprocess, performs the initialize handshake
over JSON-RPC 2.0 over stdio, and pipes tool calls through the transport. This
is **not** an in-process pytest call.

```
npx @modelcontextprotocol/inspector --cli --config mcp-config.json \
  --server continuum --method tools/list --format json
```

All 9 tools were returned (`tools/list`): `continuum_record_progress`,
`continuum_checkpoint`, `continuum_validate`, `continuum_resume`,
`continuum_intercept_action`, `continuum_complete_action`,
`continuum_fail_action`, `continuum_reconcile_action`,
`continuum_list_actions`. The read-only/mutating annotation split was as
declared (3 read-only, 6 mutating).

Test database: `.continuum/inspector-test.db`, separate from any prior history,
created fresh and deleted afterward. Authorization was granted via config-file
env (`CONTINUUM_MCP_MUTATING_CLIENTS=inspector-cli`); the caller name observed
in the handshake was `inspector-cli`, injected by the transport server-side.

Each sequence below used a new inspector invocation per call — the server
process was killed between calls (the inspector spawns a fresh subprocess per
`--method` invocation). Crashes are therefore real process deaths, not simulated
exceptions.

### Sequence A — clean crash, MCP-written state

```
record_progress(run_id='run_a_001', completed=50, total=200)
record_progress(run_id='run_a_001', completed=100, total=200)
checkpoint(run_id='run_a_001')
                        ← server process ends here
resume(run_id='run_a_001')   ← new server process, same db
```

Result (`continuum_resume` JSON):

```json
{
  "mode": "request_human",
  "safe": false,
  "next_allowed_action": "human_review:goal",
  "rationale": ["at least one repair needs a person"],
  "repairs": [
    {"action": "human_review:goal", "kind": "human_review",
     "reason": "v1, asserted by external_agent", "requires_human": true},
    {"action": "human_review:progress", "kind": "human_review",
     "reason": "100 completed, self-reported by external_agent and not independently verified",
     "requires_human": true}
  ],
  "uncertain_actions": [],
  "progress": {"completed": 100, "pending": 100, "failed": 0, "total": 200}
}
```

MCP-written state (`Origin.EXTERNAL_AGENT`) is correctly not trusted: goal and
progress are `REQUIRES_REVIEW`, mode is `request_human`. No uncertain actions —
the crash happened *after* checkpoint, not mid-action.

### Sequence B — crash between intercept and complete

```
record_progress(run_id='run_b_001', completed=0, total=100)
intercept_action(run_id='run_b_001', action_type='test.write_file', arguments={...})
                        ← server killed here; complete_action never called
resume(run_id='run_b_001')   ← new server process
list_actions(run_id='run_b_001')
```

`intercept_action` returned `proceed: true`, status `started` — the action was
claimed in the ledger. The resume JSON:

```json
{
  "mode": "request_human",
  "safe": false,
  "next_allowed_action": "reconcile_action:action_ee2437c3ddb0ed69fa8d5766c9e051bd",
  "rationale": [
    "1 external side effect(s) have unknown outcomes",
    "at least one repair needs a person"
  ],
  "repairs": [
    {"action": "reconcile_action:action_ee2437c3ddb0ed69fa8d5766c9e051bd",
     "kind": "reconcile_action",
     "reason": "test.write_file was interrupted; the side effect may or may not have occurred",
     "requires_human": false},
    {"action": "human_review:goal", "kind": "human_review",
     "reason": "v1, asserted by external_agent", "requires_human": true},
    {"action": "human_review:progress", "kind": "human_review",
     "reason": "0 completed, self-reported by external_agent and not independently verified",
     "requires_human": true}
  ],
  "uncertain_actions": [
    {"action_id": "action_ee2437c3ddb0ed69fa8d5766c9e051bd",
     "action_type": "test.write_file", "status": "started"}
  ],
  "progress": {"completed": 0, "pending": 100, "failed": 0, "total": 100}
}
```

`list_actions` confirmed the ledger state:

```json
{
  "actions": [
    {"action_id": "action_ee2437c3ddb0ed69fa8d5766c9e051bd",
     "action_type": "test.write_file",
     "external_id": null, "side_effect_uncertain": false,
     "status": "started"}
  ],
  "unresolved": 1
}
```

The action was not silently completed, not retried, not dropped. It stayed
`started`, surfaced in `uncertain_actions`, and the contract named
`reconcile_action:<id>` as the next required step. `safe: false`.

### Sequence C — trusted-writer state, clean crash

State was created in-process via `GenericAgentAdapter` (not through MCP), with
150 `WORK_COMPLETED` events folded into the checkpoint. Origin:
`DETERMINISTIC` for all components. `source_sequence: 156`.

```
# state written in-process, checkpointed, then:
resume(run_id='run_c_001', env={dataset: v1})
```

Result:

```json
{
  "mode": "resume",
  "safe": true,
  "next_allowed_action": null,
  "repairs": [],
  "progress": {"completed": 150, "failed": 0, "pending": 50},
  "contract": {
    "recovery_status": "safe_to_resume",
    "verified": ["approval:apr_001", "external_dependency:dataset", "goal", "progress"],
    "invalidated": [],
    "required_actions": []
  }
}
```

Exit code: **0**. Trusted-writer state whose environment matches resumes cleanly.

### What this establishes

The self-certification fix (`9738b9e`) behaves correctly under a real external
MCP client hitting a real process boundary — not just in pytest:

- MCP-attested state cannot self-certify safety (Sequences A, B → `request_human`)
- A crash between `intercept_action` and `complete_action` leaves the action
  uncertain and blocks resume until reconciled (Sequence B)
- Trusted-writer state resumes cleanly when warranted (Sequence C → `resume`,
  exit 0) — ruling out the alternative explanation that the system simply never
  resumes

The MCP server's two-phase action interception, ledger uncertainty handling, and
authorization gating all functioned as documented when driven through the
actual stdio protocol by an external process.

### What this does NOT establish

This was still a **scripted** test. The building agent itself acted as the MCP
client, following an exact predetermined sequence. No independent LLM (Claude
Code, Gemini CLI, etc.) has yet chosen *on its own initiative* to call
`continuum_checkpoint` or `continuum_resume` without being told the exact steps.
Whether the tool descriptions actually motivate correct **autonomous** usage by
an LLM agent — calling checkpoint at the right moment, calling resume before
acting, respecting the response — remains **open**. This is the same question
flagged in the Third-party MCP client testing section above (neither Gemini nor
Kilo completed a cycle either), and it is unanswered by this test. As of 2026-08-13
the server is confirmed reachable and fully tool-callable from a real Claude Code
session (see the section below); the autonomous-usage question above remains open.

---

## MCP server verified from Claude Code (2026-08-13)

The CONTINUUM MCP server is registered in Claude Code (config in `~/.claude.json`,
project entry: command `/opt/miniconda3/bin/python -m continuum.mcp`,
`CONTINUUM_DB=/tmp/continuum-claude-mcp.db`,
`CONTINUUM_MCP_MUTATING_CLIENTS="claude-code claude"`). It was driven end to end
from a real Claude Code session and confirmed reachable and functional.

### The Failed to connect incident and its root cause

`claude mcp list` reported `continuum ... ✘ Failed to connect` and no
`mcp__continuum__*` tools were available in-session. Traced from Claude Code's own
connection log, the server process was crashing at startup:

    sqlite3.OperationalError: disk I/O error
        at PRAGMA journal_mode=WAL   (src/continuum/storage/sqlite.py:144, SQLiteStorage._configure)
        opening CONTINUUM_DB=/tmp/continuum-claude-mcp.db

Root cause: a previously hard-killed server process left orphaned WAL sidecar files
(`/tmp/continuum-claude-mcp.db-wal`, `...-shm`) in an inconsistent state, so SQLite
refused to reopen the database in WAL mode. Registration, the editable install
(`continuum-agent`, `mcp` 2.0.0), the config, and the server code were all fine.
Pointed at a fresh database path, the server answered a manual `initialize` and
`tools/list` handshake correctly.

Remediation applied: deleted the two stale sidecar files. `claude mcp list` then
reported `✔ Connected`. This was environment and data cleanup only, no source
changed.

### What was verified through the live server

- All 9 tools exposed, correctly split by `read_only_hint`: 3 read-only
  (`validate`, `resume`, `list_actions`) and 6 mutating (`record_progress`,
  `checkpoint`, `intercept_action`, `complete_action`, `fail_action`,
  `reconcile_action`).
- Full cycle exercised for real: `record_progress` (twice) then `checkpoint`,
  `validate`, `intercept_action`, `complete_action`, `list_actions` (reflected the
  completed action, `unresolved: 0`), then `resume`. Every call returned sane JSON,
  not merely no error thrown.
- Authorization boundary intact: `claude-code` (allowlisted) may call mutating
  tools; an unlisted caller and an empty `clientInfo` are denied mutating calls
  while read-only still works. Deny-by-default posture preserved.
- A run written purely through MCP resumes as `request_human` and `safe: false`
  (progress is tagged `EXTERNAL_AGENT` and the self-certification fix `9738b9e`
  degrades it to human review). The trusted-writer clean-resume pattern
  (`mode: resume`, `safe: true`) from Sequence C was reproduced by seeding state
  in-process via `GenericAgentAdapter` (DETERMINISTIC origin) and confirming it
  resumes through MCP.

### Known limitation: startup latency

Opening the server imports `continuum.adapters`, which eagerly imports `langgraph`
(about 0.6s) and `openai` (about 0.9s); spawn to first response is about 3s. This
is within Claude Code's health-check tolerance today, but deferring the adapter
imports until first use would be a worthwhile follow-up.

### Resolved: startup self-heal for orphaned WAL sidecars

Deleting the sidecars by hand was a band-aid: the next hard-kill left them again,
so the server would fail to connect on the following launch. This is now fixed in
code (Option 1). `src/continuum/mcp/server.py` gains `_open_server_storage(database)`,
which `ContinuumMCP.__init__` uses in place of a direct `SQLiteStorage(...)` open. On
a `sqlite3.OperationalError` it removes any orphaned `<db>-wal`/`<db>-shm` sidecars
and retries opening exactly once; if no sidecar was present to remove it re-raises,
so an unrelated disk error still surfaces rather than being swallowed. The recovery
is scoped to the server startup path only: the library's `journal_mode=WAL`,
`synchronous=FULL`, and IMMEDIATE-transaction guarantees in
`src/continuum/storage/sqlite.py` are untouched.

Two regression tests in `tests/test_mcp_server.py` cover it:
`test_server_startup_recovers_from_orphaned_wal_sidecars` drives the full recovery
branch (a failing open, sidecar removal, a succeeding retry, the pre-crash run and
events still readable, and the `ContinuumMCP` constructor routing through the same
path), and `test_server_open_reraises_a_disk_error_with_no_sidecars` proves an
unrelated disk error is re-raised after a single attempt. The tests inject the
`OperationalError` while a sidecar is present rather than crafting a blocking
sidecar on disk, because a hand-written `-wal`/`-shm` pair does not reproduce the
error on every filesystem (on the APFS volume used here SQLite simply rebuilds the
sidecars and opens cleanly, verified including after a real `kill -9`). Injecting
the error drives the identical recovery branch deterministically on any filesystem.

---

## The issue #6 e2e series (2026-08-13)

The `e2e-autonomy-test/` kit answered its open question: will an unscripted LLM
agent use CONTINUUM correctly on its own? Three full runs against real Claude
Code sessions (Opus 4.8), each hard-killed mid-batch and resumed in a
brand-new session, all scored 7/7 on the mechanics checks. The autonomy half
was demonstrated too: agents called `record_progress`, routed sends through
`intercept_action` -> write -> `complete_action`, called `resume` before
acting, surfaced the `request_human` / `safe: false` verdict rather than
overriding it, and refused to re-send invoices they verified as already sent.

### The defect the runs exposed

Dedup was keyed on the caller's raw argument dictionary. Session 1 recorded
`send_invoice` with relative-path arguments (`INV-001.sent`); session 2 passed
absolute paths (`/tmp/e2e-outbox/INV-001.sent`). `idempotency_key` hashes
(action type + arguments), so the two sessions computed different keys and
`continuum_intercept_action` answered `proceed: true` for invoices already
sent. In all three runs correctness was preserved only because the agent
cross-checked the outbox and refused the flag. A less careful run would have
double-sent.

### What closed it

`continuum_intercept_action` now accepts a stable `key` that identifies the
specific operation (e.g. `invoice:INV-001`), passed through to
`ActionLedger.claim(key=...)`. The tool description tells callers to derive the
key from the resource identity, not incidental formatting. Two attempts sharing
action type and key are the same action regardless of argument shape, so dedup
is immune to relative-vs-absolute path drift and the resumed session gets
`proceed: false` instead of a fresh `started` slot to fail out. The regression
test `test_a_stable_key_deduplicates_across_argument_shape_changes` mirrors the
e2e failure exactly: intercept and complete with `key="invoice:INV-001"` and
relative arguments, then intercept again with the same key and absolute
arguments, and assert `proceed: false`.

### Defensive hardening from the transcript analysis

Re-reading the three transcripts showed the stable-key fix was necessary but
not sufficient on its own. The actual drift was richer than relative-vs-absolute
paths:

- **Argument field renames.** The send tool was called with `target`,
  `outbox_file`, `outfile`, and `file` as the path argument name across the
  runs, so even identical paths hashed differently.
- **Action type drift.** One run recorded `send_invoice` in one session and
  `send-invoice-email` in another.
- **External id shape drift.** Completion reported `/tmp/e2e-outbox/INV-004.sent`
  in one run and bare `INV-004.sent` in another.
- **The only stable identity** across every shape was the resource token itself
  (`INV-001`), surviving as a scalar value, a path basename, and an external id
  stem.

Two layers now harden dedup so correctness does not depend on the caller
supplying a key or naming arguments consistently:

1. **Path canonicalization.** `arguments_hash` and `idempotency_key` now
   normalize path-like arguments (lexical `normpath` plus `~` expansion; URLs
   untouched) before hashing, so equivalent spellings of the same path collapse.
2. **Token-based identity fallback.** When `claim()` is called with no explicit
   key and the exact hash lookup misses, it recognizes an already recorded
   action of the same type by shared identity tokens (scalar values, path
   basenames/stems, external ids). Weak tokens (counts, status words) and the
   `continuum_run_id` plumbing token never match. A unique completed match
   returns `fresh=False` with the stored result and its real recorded key; a
   unique interrupted match is surfaced as uncertain (never a fresh slot);
   multiple candidates fall through rather than guess. Action type drift is not
   bridged by design: different types are genuinely different operations.

Regression tests mirror each observed drift shape, and the full suite (700
tests) stays green.

### Secondary observations

- **Ledger pollution.** Before the fix, agents resolved the spurious
  `proceed: true` slots via `fail_action(certain=true)`. Semantically honest
  (no new side effect occurred) but it recorded `send_invoice -> failed` rows
  for invoices that actually succeeded earlier. The fix removes the spurious
  slots, so no such rows are created.
- **Checkpoint version, still open.** Session 1 reported taking a checkpoint
  (`checkpoint_a03ba166...`), but `continuum_resume` consistently reported
  `checkpoint_version: 0` on resume. Whether the resume contract reflects
  checkpoints at all is not established. Not filed yet.

### The "failed" rows note

The verify scorecard counts only `completed` and `unresolved`, so the
workaround `failed` rows never failed a run. They were a data-quality smell,
not a correctness failure, and are gone with the fix.

---

## Unresolved

`demo_report.md` (an untracked artifact from the third-party testing above)
changed from 4,997 bytes with five sections to 746 bytes with one, between two
examinations during a single working session. Nothing in this repository
accounts for it.

At the time, `claude` and `gemini` CLI processes were running on other TTYs,
and two `kilo serve` processes had been running all day. **Concurrent agent
sessions are the most plausible explanation, but this was inferred from process
listings and timestamps — it was never confirmed.** It is recorded here as an
open question rather than a closed one.

A related, confirmed observation: files in this repository were modified during
this work by processes other than the session doing the work — including an
`adapters/` package and a `recovery/engine.py` branch that appeared
mid-session. If state seems to change without explanation, check for other
agent processes before assuming a bug.

---

## Repository housekeeping

The commit history on `main` is dominated by website and logo iteration: roughly 50 of ~115 commits are site, favicon, or logo experiments, including one revert (`ea583ec`). This is cosmetic churn, not open code debt. Not tracked as a GitHub issue: rewriting history on a repository others may have forked is disruptive, and a public flame-on-commits is low value. If a clean history matters for the v0.1.0 presentation, squash or rewrite those commits before release; otherwise leave them.

---

## Untracked files, deliberately excluded

- `.mcp.json` — Claude Code registration; hard-codes machine-specific absolute
  paths.
- `demo_report.md` — artifact from third-party client testing.
- `kilo.jsonc` — Kilo's own MCP config, written by Kilo.

---

## Security Extension (in progress, not part of v0.1.0 launch)

Two additive extensions are being prototyped on top of the existing
recovery/checkpoint substrate. Both are additive: they do not change resume,
replay, or the existing crash-time revalidation path. Deliberately scoped to
avoid scope creep (no adversarial training, no new policy language).

- `docs/PROBLEM.md` — the problem statement each extension addresses, with the
  paper, the date, the unmet claim, and our honest "does not solve" framing.
- `src/continuum/security/provenance.py` — `ObservationProvenance` and
  `PlanBranch` (frozen pydantic v2, matching `models.py` conventions).
- `src/continuum/security/trust_gate.py` — `verify_observation` (two-signal
  trust: `verified` / `unverified` / `contested`), `record_observation`,
  `resolve_branch` (risk-tiered escalation to `REQUIRES_REVIEW` for high-risk
  unverified/contested, and contested environment observations), `ReviewGate`.
- `src/continuum/security/revalidation.py` — `RevalidationTrigger`,
  `RevalidationPolicy`, `RevalidationResult`, `maybe_revalidate` (fires on a
  step interval and on app switch, reusing `RecoveryEngine.assess`).
- `src/continuum/security/prompts/secure_planning.md` — the planner prompt
  contract for Extension 1.
- `docs/RESULTS.md` — numbers; the mini-benchmark is still PENDING (runs after
  the core mechanism is proven).

Tests: `tests/test_trust_gate.py`, `tests/test_revalidation_schedule.py`,
`tests/test_toy_task_banner_attack.py` (a cookie-consent banner before/after
pair). All 700 tests pass; `ruff` and `mypy --strict` are clean.

What this does NOT claim: Extension 1 does not defeat an optimized pixel-patch
attack (still open per CaMeLs), it adds an audit trail and escalation.
Extension 2 does not improve long-horizon reasoning, it re-checks ground truth
on a schedule. Neither claims to have "solved" its source paper.

# Project status

**As of 2026-08-21** (commit `8932bfd` and later). On 2026-08-14 a repository-wide bug
audit ran: every behavioural module was read and exercised, surfacing 20
evidence-backed issues (#29-"#49, excluding the externally-filed #39). They are
labelled `good first issue` or `help wanted` (plus `adapter`/`detector` where
relevant) and formed the contributor backlog. All twenty have since been fixed
and merged to `main`; the Known issues at launch table below records each one as
Resolved. (An earlier revision of this file described issues `#29`, `#30`,
`#33`, `#34`, `#36`, `#42`, `#43`, `#45` and `#49` as open contributor work;
that was stale, and a 2026-08-22 audit confirmed all nine closed on GitHub with
their fixes on `main`.)

A factual snapshot for whoever picks this up next, human or otherwise, with no
memory of how any of it was found. It records what is verified, what is
believed, and what is neither.

---

## In flight: unprojectable logs degrade instead of dying (#383, PR #385)

As of 2026-08-25, PR #383's fix is open on `fix/degrade-unprojectable-fold`
(three commits), reviewed through two rounds. The fold accepts
`on_unprojectable="raise"|"degrade"` (default byte-for-byte unchanged);
degrade returns the last-good prefix marked `SemanticState.status=INVALID`
naming where folding stopped; recovery decides REQUEST_HUMAN and the contract
carries a `repair_log` step. Checkpoint digests and persisted bodies exclude
the projection-bookkeeping fields, so databases written before or after the
change load either way (cross-version tests pin serialised fixtures in
`tests/test_checkpoint_compat.py`). Verified on the branch: 1425 passed,
23 skipped, ruff clean, strict mypy clean on 104 files. Repair/amend (option
2) and fork-from-last-good-prefix (option 3) remain unbuilt by design.

---

## Full-gate audit (2026-08-24)

Ran against `main` at `8013f6a` in a clean worktree, Python 3.13
(104 source files, 99 test files):

- `pytest`: **1345 passed, 24 skipped, 0 failed** (35s). The suite is green.
- `ruff check src/ tests/ examples/`: pass.
- `ruff format --check`: pass (216 files). The gate had gone red after #275
  landed a non-canonical block in `cmd_resume`; it was repaired directly on
  main (`11905e3`, `7fe38d3`) before the open fix PR could land, which left
  #298 without anything to fix.
- `mypy src/continuum`: pass, evidenced by the `Lint & Type-check` CI job on
  recent main runs rather than a local interpreter (local mypy versions skew).
- Distribution surfaces verified live: #277 merged, and the GHCR publish
  workflow ran green on both the #275 and #277 merges, so
  `docker run --rm ghcr.io/cyrax321/continuum` serves the crash-recovery demo
  from a published image.

## Verified

1047 tests collected, 1038 passing, 9 skipped, on Python 3.13 with `mcp 2.0.0` installed. The MCP
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
| Checkpoints | `checkpoint/` | Policy-driven (manual, interval, event, semantic, context-pressure, hybrid). Restore replays events recorded after the checkpoint. Phase 4 added `RECOVERY` anchors (`checkpoint_on_recovery`), `last_recovery_anchor` lookup, `prune` (keeps newest + anchors), and `Storage.delete_checkpoint` (SQLite + Postgres); `StateCheckpoint.reason` is now stored. |
| Validation | `state/validator.py` | Checks state against the current environment. Staleness propagates `dependency -> evidence -> finding -> decision`. |
| Action ledger | `actions/` | Idempotent claim/complete. Raises `UnknownSideEffect` rather than guessing when an outcome is unknown. |
| Recovery engine | `recovery/` | Reduces validation, ledger and checkpoint signals to one `RecoveryMode`. Takes the **maximum** on a severity ordering, so the most cautious signal wins regardless of evaluation order. |
| Recovery ledger | `recovery/ledger.py` | Append-only, hash-chained audit of recovery decisions. `verify` reports the last trusted index (tamper-evident), `compact` drops old entries while preserving anchors and re-sealing the chain, `record_gate`/`pending_gate` persist human-in-the-loop decisions, `requires_human` enforces an attempt budget, and `reconcile` detects state-vs-ledger drift. Optional `LeaseCoordinator` for cross-process safety. |

### Interfaces

- **CLI** (`cli/`), 33 commands at `main` (`4453c72`, recounted by enumerating
  the built parser on 2026-08-24), stdlib `argparse` only. Exit codes are a
  safety contract: only a verified-safe run exits `0`, so
  `continuum resume "$RUN" && ./start-agent.sh` cannot launch onto stale state.
  Colour is TTY-aware and respects `NO_COLOR`; piped output is byte-identical
  to uncoloured output.
- **`GenericAgentAdapter`** (`adapters/generic.py`), in-process Python facade.
- **`LangGraphAgentAdapter`** (`adapters/langgraph.py`), LangGraph
  integration, optional `langgraph` dependency.
- **`OpenAIAgentAdapter`** (`adapters/openai.py`), OpenAI Agents SDK
  integration, optional `openai-agents` dependency.
- **MCP server** (`mcp/server.py`), 11 tools over stdio: 3 read-only by
  `read_only_hint` annotation (`continuum_validate`, `continuum_resume`,
  `continuum_list_actions`) and 8 mutating, recounted from the tool
  registrations on 2026-08-24. `continuum_confirm` was added alongside the
  `REVIEW_CONFIRMED` event in the launch fixes.

### MCP two-phase action interception

A Python callable cannot cross the MCP boundary, so the server cannot execute a
side effect on the caller's behalf. The protocol is:

1. `continuum_intercept_action`, claims the ledger entry, answers *may I?*
2. the caller performs the effect
3. `continuum_complete_action`, records the outcome

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
That was true of the *fold* and false of the *claim*, faithfully folding a
fabricated event yields a faithful projection of a lie. `Origin` and
`Provenance` already existed, but neither the validator nor the recovery engine
consulted them, and `Goal`/`Progress`, the two fields the exploit falsifies,
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
unauthorized caller invoking mutating tools in the first place, that is the
authorization layer below.

---

## The MCP authorization layer (`d9365c8`)

Any client that could reach the server could call any tool. Several agents have
been configured against this project's database simultaneously, Kilo, Gemini
CLI and Claude Code all pointed at the same `continuum.db`, so any of them
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

A malformed policy file raises rather than falling back, a file that exists is
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
defend against a deliberately impersonating or malicious local process, which
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
developed attempt at the same fix, with the right shape, handshake identity,
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
produced a green checkmark, and its `Fixes #1` footer would have auto-closed
the issue on merge. Passing tests, a closed issue, and an open hole is a worse
outcome than no fix at all.

Two things were kept from it: the `CONTINUUM_MCP_MUTATING_CLIENTS` name, and the
observation that raising `ToolError` directly is a defensible alternative to the
`PermissionError` subclass used here.

---

## Previously open items

| Issue | Summary | Priority |
|:--|:--|:--|
| [#1](https://github.com/Cyrax321/CONTINUUM/issues/1) | **MCP caller authentication.** Authorization for mutating tools (added in `d9365c8`) denies by default; what was missing was authentication, `clientInfo` was client-asserted and unverified. Now resolved: when `CONTINUUM_MCP_TOKEN` is set, the server refuses every mutating tool unless the caller presents that shared secret in the handshake's `_meta.authToken`. Fail-closed (missing or mismatched secret always refuses; an empty configured secret refuses rather than opening the door, the PR #3 mistake). Default local behavior is unchanged when the variable is unset. | Medium | Resolved, `AuthPolicy`/`load_auth` in `src/continuum/mcp/authz.py`, wired into the tool `guard` in `src/continuum/mcp/server.py`; tests in `tests/test_mcp_authz.py` (including `test_auth_fails_closed_when_required_but_unset`). |

### Code audit findings (2026-08-12)

A module-by-module audit filed seven issues, each reproduced against clean
`HEAD` (455e307) and filed with the `bug_report` template:

| Issue | Summary | Priority | Status |
|:--|:--|:--|:--|
| [#15](https://github.com/Cyrax321/CONTINUUM/issues/15) | **Over-total progress is a partial write.** `record_progress`/event writers commit a `TASK_UPDATED` whose `completed + pending + failed > total`; the log then passes `verify_events` but every projection, checkpoint, resume and validate raises a raw pydantic `ValidationError`, permanently, with no rollback. | High | Resolved, `91aee41` rejects over-total progress before it is written, raising `ToolError`/`ValidationError` at the boundary rather than committing a corruptible event. |
| [#20](https://github.com/Cyrax321/CONTINUUM/issues/20) | **Read-only `list_actions` writes.** Annotated `read_only` (and therefore ungated), `continuum_list_actions` calls `ensure_run`, backfilling `RUN_STARTED` into a bare run's log. Contradicts the read-only split guarantee. | High | Resolved, `71c86b3` resolves the run via `get_run` instead of `ensure_run`, so a bare run lists zero actions without appending anything. |
| [#16](https://github.com/Cyrax321/CONTINUUM/issues/16) | **STALE STATE section droppable.** `build_recovery_context` protects sections by sorted index, not identity: with `next_action` present, the STALE STATE section falls outside the `protected = 3` window and is dropped under a tight budget despite the never-dropped promise. | High | Resolved, `e9c5f78` protects the never-dropped sections (`CURRENT GOAL`, `VERIFIED PROGRESS`, `STALE STATE, DO NOT RELY ON`) by identity via a `_NEVER_DROPPED` set, so an injected higher-priority section cannot push stale state out of the protected set. |
| [#21](https://github.com/Cyrax321/CONTINUUM/issues/21) | **OpenAI adapter cannot auto-provision runs.** `_ensure_run_exists` reads via `get_run` which raises rather than returning `None`, so its `create_run` branch is dead code and `on_agent_start` raises `RunNotFound` for any fresh run. | Medium | Resolved: `_ensure_run_exists` now catches `RunNotFound` and creates the run, so a fresh OpenAI agent run is auto-provisioned on first contact; two regression tests in `tests/test_adapters_openai.py` cover the create-on-missing and idempotent-exists paths. |
| [#17](https://github.com/Cyrax321/CONTINUUM/issues/17) | **Older-schema DB accepted silently.** A pre-v2 file opens without `SchemaVersionError` (only newer versions are rejected), `read_events` returns `[]` for a populated run, and the first write fails with a raw sqlite `OperationalError`. No migration path exists. | Medium | Resolved, `82b9f1c` raises `SchemaVersionError` at open when the stored schema version is below `SCHEMA_VERSION`; adds `tests/test_storage.py::test_an_older_schema_is_refused`. |
| [#19](https://github.com/Cyrax321/CONTINUUM/issues/19) | **`resume --repair` is a no-op.** Help and docstrings claim `--repair` records the repair plan (and is one of only three mutating commands); in practice it only suppresses a stderr hint, writing nothing. | Medium | Resolved, `f145818` makes `cmd_resume` append a `RECOVERY_STARTED` event carrying the plan steps when `--repair` is given and a plan exists; adds `tests/test_cli.py::test_repair_records_the_plan_and_does_not_fake_a_safe_exit` and `tests/test_cli.py::test_resume_without_repair_is_still_read_only`. |
| [#18](https://github.com/Cyrax321/CONTINUUM/issues/18) | **`events` breaks the exit-code contract.** `continuum events $MISSING` exits 0 with "No events.", while every other run-scoped command exits 2; `events` is absent from the enforcing parametrised test. Tagged `good first issue`. | Medium | Resolved: `1bcc933` gates `cmd_events` on `get_run` (which raises `RunNotFound`, mapped to `NOT_FOUND` by the dispatcher) and adds `events` to the missing-run parametrised test. |
| Orphaned-WAL startup crash | **MCP server fails to connect after a hard-kill.** A killed server leaves `<db>-wal`/`<db>-shm` sidecars that make `PRAGMA journal_mode=WAL` throw `disk I/O error` on the next launch. | Medium | Resolved: `_open_server_storage` in `src/continuum/mcp/server.py` clears orphaned sidecars and retries the open once on `OperationalError` (re-raising when there was nothing to clear), with two regression tests in `tests/test_mcp_server.py`. See the MCP server section. |
| Issue #6 e2e dedup defect | **`continuum_intercept_action` deduplicated on raw argument formatting, not resource identity.** Three real Claude Code e2e runs showed session 2 getting `proceed: true` for invoices session 1 already sent, because relative-path vs absolute-path arguments hashed to different idempotency keys. Correctness survived only because the agents cross-checked the outbox and refused the flag. | High | Resolved twice over: the tool accepts a stable `key` (e.g. `invoice:INV-001`) that is what makes two attempts the same action, and a defensive layer now covers the no-key and argument-drift cases (path canonicalization plus a token-based identity fallback in `ActionLedger.claim`). Regression tests: `test_a_stable_key_deduplicates_across_argument_shape_changes` plus the identity-match and canonicalization tests in `tests/test_action_ledger.py`. |
| Stale editable metadata | `pip show continuum-agent` reported its editable location as `Desktop/untitled folder 2` (the pre-move path); imports still resolved correctly, so it was cosmetic. A clean-venv reproduction at `b7d07b8` reported the current repository root as the editable location and imported `continuum` from its `src` tree, confirming the package configuration is correct. | Low | Resolved: uninstall/reinstall remediation documented in `CONTRIBUTING.md`. |

### Launch audit (2026-08-14)

A second module-by-module audit filed 20 issues (#29-#49, minus external #39).
The three launch-critical defects were fixed and closed in `e8271bd`:

- **#35**, MCP self-certified progress: added `REVIEW_CONFIRMED` event,
  `continuum confirm` CLI, and `continuum_confirm` MCP tool; `StateValidator`
  and `RecoveryEngine.assess` clear self-certified `REQUIRES_REVIEW` once a
  confirmation event exists.
- **#46**, LangGraph synthetic state: `checkpoint_node` now projects real
  state from the event log when events exist, instead of emitting an empty dict.
- **#47**, OpenAI adapter `RUN_STARTED` backfill: `_ensure_run_exists` now
  backfills `RUN_STARTED` like `ContinuumMCP.ensure_run`.

The remaining **9 were real but non-launch-critical**. All are now closed on
GitHub and their fixes are on `main`:

issues `#29`, `#30`, `#33`, `#34`, `#36`, `#42`, `#43`, `#45` and `#49`.

#### Known issues at launch

Dispositions updated 2026-08-22 against `main` (`d257d85`): every row below is
resolved; where no closing commit was recorded here, the fix was confirmed by
reading the current code cited in the note.

| Issue | One-line impact | Disposition |
|:--|:--|:--|
| [#29](https://github.com/Cyrax321/CONTINUUM/issues/29) | `ActionLedger.reconcile(occurred=False)` leaves stale `external_id`/`result` on the action | Resolved: `reconcile` now clears `external_id`, `result` and `result_hash` on the occurred-false path (`src/continuum/actions/ledger.py`) |
| [#30](https://github.com/Cyrax321/CONTINUUM/issues/30) | `FileProvider` reports a missing file as `version=None`, so diff marks it `changed` not `removed` | Resolved: `FileProvider.capture` omits missing files entirely, so the diff classifies them REMOVED (`src/continuum/environment/snapshot.py`) |
| [#31](https://github.com/Cyrax321/CONTINUUM/issues/31) | `continuum replay` claims to confirm state matches the stored version but never compares | Resolved: `a5c3307` (PR #50: replay now actually verifies the stored version) |
| [#32](https://github.com/Cyrax321/CONTINUUM/issues/32) | `continuum replay --upto N` crashes with `ProjectionError` when the prefix excludes `RUN_STARTED` | Resolved: `fd1bf90` (reject `--upto` values that exclude `RUN_STARTED`) |
| [#33](https://github.com/Cyrax321/CONTINUUM/issues/33) | `identity_tokens` drops plain-word resource ids (`invoice`) because `_is_strong_token` requires a digit/`@`/`.` | Resolved: `_is_strong_token` accepts plain words of sufficient length (`src/continuum/actions/idempotency.py`, comment cites this issue) |
| [#34](https://github.com/Cyrax321/CONTINUUM/issues/34) | `ActionLedger scoped_to_run=False` does not enforce global uniqueness across runs as documented | Closed by documentation: the `idempotency_key` docstring now states plainly that cross-run uniqueness is not enforced and what would be required to enforce it (`src/continuum/actions/idempotency.py`) |
| [#36](https://github.com/Cyrax321/CONTINUUM/issues/36) | `identity_tokens` drops purely-numeric resource ids, so cross-session fallback fails on numeric ids | Resolved: `identity_tokens` collects integer scalars as tokens (`src/continuum/actions/idempotency.py`, comment cites this issue) |
| [#37](https://github.com/Cyrax321/CONTINUUM/issues/37) | OpenAI adapter: tool arguments misbound and idempotency bypassed because `__signature__` drops `ctx` | Resolved: `5acd0be` (forward idempotency key and fix OpenAI tool wrapping; also `8be8b7f` for the model-validator leg) |
| [#38](https://github.com/Cyrax321/CONTINUUM/issues/38) | `continuum_record_progress` accepts negative `completed`/`failed` when `total` is omitted, poisoning the event log | Resolved: `fca1b6e` (PR #51: reject negative progress counters even when total is omitted) |
| [#40](https://github.com/Cyrax321/CONTINUUM/issues/40) | `LLMExtractor`: malformed LLM proposal crashes `extract()` instead of falling back | Resolved: `8c7cfec` (PR #56: fall back to deterministic state on malformed LLM proposal) |
| [#41](https://github.com/Cyrax321/CONTINUUM/issues/41) | `LLMExtractor._merge` double-adds duplicate ids within a single proposal | Resolved: `a1bdef4` (PR #52: collapse ids repeated within a single LLM proposal) |
| [#42](https://github.com/Cyrax321/CONTINUUM/issues/42) | Strict mode: uncertain side effect yields `REQUEST_HUMAN` but an auto-reconcile step silently ignores `strict_unknown` | Resolved: `plan_repairs` marks reconcile steps `requires_human` when strict mode is on (`src/continuum/recovery/planner.py`, comment cites this issue) |
| [#43](https://github.com/Cyrax321/CONTINUUM/issues/43) | Two checkpoints at the same state version collapse to one in `continuum history` | Resolved: `cmd_history` lists every checkpoint row instead of keying by version (`src/continuum/cli/main.py`, comment explains why) |
| [#44](https://github.com/Cyrax321/CONTINUUM/issues/44) | `intercept_action` returns a divergent value on cache hit when the result dict holds reserved key `__return_value__` | Resolved: `15e0d67` (PR #53: keep a result dict holding the envelope key intact on cache hit) |
| [#45](https://github.com/Cyrax321/CONTINUUM/issues/45) | `claim(on_unknown=)` resolution is not persisted, so the ledger stays uncertain after call-time resolution | Resolved: `claim` records an `on_unknown` resolution as an `ACTION_RECONCILED` event so it outlives the call (`src/continuum/actions/ledger.py`) |
| [#48](https://github.com/Cyrax321/CONTINUUM/issues/48) | `StateValidator._check_progress` relabels self-certified progress as `UNKNOWN`, so `--tolerate-unknown` silently unblocks it | Resolved: `1327be3` (PR #55: self-certified progress no longer relabelled `UNKNOWN`) |
| [#49](https://github.com/Cyrax321/CONTINUUM/issues/49) | `StateValidator._check_model` reports model-specific assumptions `VALID` when `expected_model` is `None` (fail-open) | Resolved: `_check_model` reports UNKNOWN when assumptions exist but either model is unknown (`src/continuum/state/validator.py`) |

None of these blocked the v0.1.0 launch; all are now resolved on `main`.

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
[31534363260](https://github.com/Cyrax321/CONTINUUM/actions/runs/31534363260), all four jobs green: ruff lint, ruff format, mypy strict, and tests on
Python 3.11 / 3.12 / 3.13.

### Not done

No action version was bumped to a prerelease, draft, or non-semver tag. All
selected versions are the highest stable semver release for each action as of
2026-08-12.

---

## Not built

Phases 13–14 of the original plan: cloud API, dashboard. The minimal
CONTINUUM-Bench harness (Phase 12) now ships: `continuum benchmark` runs and
prints measured numbers across five recovery scenarios (`process_crash`,
`dataset_change`, `unknown_side_effect`, `partial_completion`, `early_crash`) and three
strategies, plus a dedicated issue #6 idempotency scenario that drives the real
ActionLedger under argument drift (absolute vs relative path). The Phase 14
dashboard now ships (`--dashboard`) from the A2 observability work. The fuller
published-baselines suite remains a Phase 12 goal.

The #6 proof is reproducible and not asserted: `continuum benchmark` reports 0
duplicate side effects for both `continuum_key` (stable key) and
`continuum_drift` (drift recognition), while `naive_retry` and `replay` repeat
every side effect (50 duplicates for 50 actions attempted twice). See the
regression test `tests/test_benchmark.py`.

### Framework adapters (Phase 11)

`adapters/` now contains `base.py`, `generic.py`, `langchain.py`, `langgraph.py`,
and `openai.py`. The three framework adapters are optional dependencies,
`langchain`, `langgraph` and `openai-agents` are not pulled in by
`pip install continuum-agent`; install via `pip install continuum-agent[langchain]`,
`[langgraph]` or `[openai]`. Each was written after
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
v2.1.0 in `--cli` mode, which drives the real stdio protocol boundary, the
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

Each sequence below used a new inspector invocation per call, the server
process was killed between calls (the inspector spawns a fresh subprocess per
`--method` invocation). Crashes are therefore real process deaths, not simulated
exceptions.

### Sequence A, clean crash, MCP-written state

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
progress are `REQUIRES_REVIEW`, mode is `request_human`. No uncertain actions,
the crash happened *after* checkpoint, not mid-action.

### Sequence B, crash between intercept and complete

```
record_progress(run_id='run_b_001', completed=0, total=100)
intercept_action(run_id='run_b_001', action_type='test.write_file', arguments={...})
                        ← server killed here; complete_action never called
resume(run_id='run_b_001')   ← new server process
list_actions(run_id='run_b_001')
```

`intercept_action` returned `proceed: true`, status `started`, the action was
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

### Sequence C, trusted-writer state, clean crash

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
MCP client hitting a real process boundary, not just in pytest:

- MCP-attested state cannot self-certify safety (Sequences A, B → `request_human`)
- A crash between `intercept_action` and `complete_action` leaves the action
  uncertain and blocks resume until reconciled (Sequence B)
- Trusted-writer state resumes cleanly when warranted (Sequence C → `resume`,
  exit 0), ruling out the alternative explanation that the system simply never
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
an LLM agent, calling checkpoint at the right moment, calling resume before
acting, respecting the response, remains **open**. This is the same question
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

### Resolved: startup latency (#214)

This section previously recorded spawn-to-first-response of about 3s, caused by
`continuum.adapters` eagerly importing `langgraph` (about 0.6s) and `openai`
(about 0.9s). Closed by issue #214: optional SDK adapter names now resolve
lazily through module `__getattr__` (PEP 562) in
`src/continuum/adapters/__init__.py`, so processes that never touch a framework
adapter (the MCP server, each `continuum observe` hook subprocess) no longer
pay for them, while `from continuum.adapters import X` keeps working for every
public name. The dependency-free adapters stay eager.

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

Regression tests mirror each observed drift shape, and the full suite (740
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

## Real-LLM framework adapter test (all adapters) + OpenRouter (2026-08-15)

Every adapter integration test in this tree (`tests/test_integration_langgraph.py`,
`tests/test_integration_langchain.py`, `tests/test_integration_langchain_agent.py`,
`tests/test_integration_langchain_agent.py`) drives a real agent *loop* but with a
**scripted fake model** (`_ScriptedLLM` / offline `create_agent`). They prove the
adapter mechanics against a genuine framework runtime, but they never touch a real
LLM. The open question this answers: does each framework adapter behave correctly
when a live model, not a script, is calling the tools?

### What we did

- Drove all three framework adapters (LangChain, OpenAI Agents SDK, LangGraph)
  against a live `gpt-4o-mini` through OpenRouter (`base_url="https://openrouter.ai/api/v1"`,
  key from `OPENROUTER_API_KEY`, never written to disk).
- Added `key` / `key_fn` forwarding to every adapter (LangChain, LangGraph, OpenAI)
  so a stable idempotency key collapses a live model's argument drift to exactly-once.
- Fixed two OpenAI-adapter bugs that only surfaced with a real model: the tool JSON
  schema was emitted with no `type` key (OpenRouter rejected it), and the context
  parameter was dropped from the inspectable signature, which bypassed interception
  and let the side effect fire twice.
- Built two kinds of live proof per adapter: a *soft-resume* run (exactly-once side
  effect across a second clean invocation) and a *hard-crash* run (`os._exit(137)`
  mid-side-effect, then a fresh process asserts the run is blocked as uncertain).
  Plus a richer multi-step live demo that orchestrates lookup + notify + ticket
  through the LangGraph adapter.

### Test results (live `gpt-4o-mini` via OpenRouter)

| Adapter    | Soft resume (exactly-once)             | Hard crash (resume blocked)       |
|------------|----------------------------------------|-----------------------------------|
| LangChain  | PASS - 1 side effect, `resume` safe    | PASS - `request_human`, 1 uncertain |
| OpenAI SDK | PASS - 1 side effect, `request_human`* | PASS - `request_human`, 1 uncertain |
| LangGraph  | PASS - 1 side effect, `resume` safe    | PASS - `request_human`, 1 uncertain |

\* The OpenAI adapter yields `request_human` (not `resume`) even on a clean
soft-resume because it records `Origin.EXTERNAL_AGENT`; an agent must not
self-certify its own unverified work. That is expected and safe. LangChain and
LangGraph use `Origin.DETERMINISTIC`, so they resume cleanly.

Multi-step live demo (`examples/multitool_real_llm.py`, LangGraph): PASS - the model
orchestrated `lookup_order` + `notify_customer` + `create_ticket`, recovery returned
`resume / safe=True` for both passes, and exactly-once held even though the model
rendered `order_id` two different ways in a single pass. A *fixed* business key
(`notify:O-9`, `ticket:O-9`) was required; a key derived from the model's argument
produced a duplicate ticket and is therefore unsafe under drift.

The detailed per-adapter runs, the crash proofs, and the multi-step demo follow
below.

### Setup

- `langchain-openai` was not installed; installed it so `ChatOpenAI` is available.
- `examples/langchain_real_llm.py` wraps `ChatOpenAI` against OpenRouter's
  OpenAI-compatible endpoint (`base_url="https://openrouter.ai/api/v1"`), model
  `openai/gpt-4o-mini`, temperature 0.
- The adapter starts a run (`Origin.DETERMINISTIC`), wraps a `notify.customer` tool
  with `wrap_tool`, and a `BaseCallbackHandler.on_tool_end` calls
  `checkpoint_node` after every tool result. The agent is then invoked twice over
  the same run: a first pass, then a "resume" with the same task.

### First run: exactly-once broke under a real model

Without a stable key, `wrap_tool` keys on the argument hash. The live model did not
pass `order_id="O-9"`. It stuffed a generated sentence into the parameter, and the
two invocations produced *different* argument strings, so the hash differed and the
ledger opened a fresh slot each time. Side effect fired twice:

```
== First invocation (model: openai/gpt-4o-mini) ==
agent: The customer has been notified about their order O-9. ...
recovery after run 1: resume safe= True
external side effects so far: 1

== Resume: same run, second invocation ==
agent: The customer for order O-9 has been successfully notified. ...
recovery after run 2: resume safe= True
external side effects total (must be 1): 2     <-- WRONG
event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
    3 ACTION_RECORDED
    4 STATE_CHECKPOINTED
    5 ACTION_RECORDED
    6 ACTION_RECORDED
    7 STATE_CHECKPOINTED
```

This is the issue #6 argument-drift class (an LLM rendering the same operation with
inconsistent argument text), surfacing through a framework adapter instead of the
MCP server. The fix for MCP was a stable `key`; the adapters simply never exposed
it. `GenericAgentAdapter.intercept_action` and `LangChainAgentAdapter.wrap_tool`
now forward `key` (a fixed string) and `key_fn` (derived from the call's
`(*args, **kwargs)`) down to `ActionLedger.claim(key=...)`.

### Second run: exactly-once holds with a stable key

Re-run with `@adapter.wrap_tool("notify.customer", key="notify:O-9")`. The model
again emitted different argument text each invocation, but the explicit key
identifies the operation, so the second call returns the cached result without
re-executing the effect:

```
== First invocation (model: openai/gpt-4o-mini) ==
agent: The customer has been notified about their order O-9. ...
recovery after run 1: resume safe= True
external side effects so far: 1

== Resume: same run, second invocation ==
agent: The customer for order O-9 has been successfully notified. ...
recovery after run 2: resume safe= True
external side effects total (must be 1): 1     <-- CORRECT
event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
    3 ACTION_RECORDED
    4 STATE_CHECKPOINTED
    5 STATE_CHECKPOINTED

OK: exactly-once side effect preserved across resume.
```

The event log now shows exactly one claim/complete pair (events 2, 3); the resume
produced no new `ACTION_RECORDED` events, only a second checkpoint. The recovery
engine assessed `resume / safe=True` for the deterministic-origin run both times.

### What this establishes

- `LangChainAgentAdapter` works against a real LLM: the tool-calling loop ran,
  side effects were intercepted through the ledger, and checkpoints were persisted
  on every tool result, all over a live model rather than a script.
- Exactly-once survives real LLM argument drift **only when an explicit key is
  used**. Argument-hash dedup alone is not sufficient for LLM-driven tools; this
  is the same lesson the MCP `key` parameter already encoded, now available in the
  adapters.
- Regression tests in `tests/test_integration_langchain.py`
  (`test_explicit_key_deduplicates_against_argument_drift`,
  `test_key_fn_derives_key_from_call_arguments`) lock the behaviour in without a
  network: the same logical operation rendered with drifted argument text collapses
  to one external side effect. The full suite stays green and `ruff` / `mypy` are
  clean.

### What this does NOT establish

- Only the **LangChain** adapter was driven with a live model in the first run. The
  OpenAI Agents SDK and LangGraph adapters have since been driven by a live model too
  (see the sections above), and all three now forward the explicit `key` / `key_fn`.
  All three adapters' hard-crash path has since been driven live as well (the
  crash-and-resume proofs above), where a mid-side-effect `os._exit(137)` leaves the
  action uncertain and blocks resume. The original real-LLM plan's open items are
  now closed: every framework adapter has live soft-resume and live hard-crash
  coverage.
- No crash was injected mid-run. The full crash-after-checkpoint / resume matrix
  per adapter is still open; this run proves the happy path plus a soft resume.
- The `key` was supplied by the harness (`key="notify:O-9"`), not chosen by the
  model. Whether an LLM can reliably derive a stable resource key on its own is
  untested. The `key_fn` form pushes that responsibility to the caller, which is
  the safer default given the drift observed above.
- The model was not asked to respect a `request_human` verdict. That autonomous
  behaviour was observed earlier over MCP (issue #6) but not re-exercised here.

### OpenAI Agents SDK adapter: real-LLM run (2026-08-15)

The LangChain harness above proved the pattern, but the OpenAI adapter had never
been driven by a live model. `examples/openai_real_llm.py` does that: it points an
`AsyncOpenAI` client at OpenRouter, wraps the model in the SDK's
`OpenAIChatCompletionsModel` (chat completions endpoint, which OpenRouter fully
supports), wraps a `notify.customer` tool with `wrap_function_tool(..., key="notify:O-9")`,
and runs the agent twice over the same run.

The first attempt failed in two ways that only a real model exposes:

1. **Invalid tool schema.** The generated wrapper typed every parameter as `Any`, so
   the SDK emitted a tool JSON schema with no `type` key. OpenRouter rejected it
   (`invalid_function_parameters: schema must have a 'type' key`). Fixed by
   preserving the original annotations in the generated source via
   `inspect.formatannotation`.
2. **No context, no interception.** The adapter overrode `__signature__` to drop the
   `ctx` parameter. `function_schema` inspects the signature to decide whether the
   tool takes a `RunContextWrapper`; without a `ctx: RunContextWrapper` first
   parameter it concluded the tool took no context and passed the raw tool-input
   string as the first positional argument. `run_id` extraction then returned
   `None`, interception was skipped, and the side effect fired directly (and twice).
   Fixed by keeping `ctx` first in the inspectable signature, annotated
   `RunContextWrapper`.

With both fixed, the live run behaves correctly:

```
== First invocation (model: openai/gpt-4o-mini) ==
agent: The customer has been notified about their order O-9.
recovery after run 1: request_human safe= False
external side effects so far: 1

== Resume: same run, second invocation ==
agent: The customer for order O-9 has been notified successfully.
recovery after run 2: request_human safe= False
external side effects total (must be 1): 1

event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
    3 ACTION_RECORDED
    4 STATE_CHECKPOINTED
    5 STATE_CHECKPOINTED

OK: exactly-once side effect preserved across resume.
```

The recovery verdict is `request_human` (not `resume`) because the OpenAI adapter
records state with `Origin.EXTERNAL_AGENT` provenance, by design (an agent must not
self-certify its own unverified work). That is the expected, safe behaviour, and it
matches the MCP-reported-run path. What this run establishes: the OpenAI adapter
intercepts a real model's tool call through the ledger, the `key` collapses the
resume to one side effect, and checkpoints are written on every tool result.

### LangGraph adapter: real-LLM run (2026-08-15)

`examples/langgraph_real_llm.py` drives the LangGraph adapter the same way, using
LangChain's `create_agent` (the current `langgraph.prebuilt.create_react_agent`
home) over `ChatOpenAI` pointed at OpenRouter, with the notify tool wrapped via
`wrap_tool(..., key="notify:O-9")` and a callback that checkpoints on every tool
result. The model is free to render the order id however it likes; the stable key
is what keeps the side effect idempotent:

```
== First invocation (model: openai/gpt-4o-mini) ==
agent: The customer has been notified about their order O-9. ...
recovery after run 1: resume safe= True
external side effects so far: 1

== Resume: same run, second invocation ==
agent: The customer for order O-9 has been successfully notified. ...
recovery after run 2: resume safe= True
external side effects total (must be 1): 1

event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
    3 ACTION_RECORDED
    4 STATE_CHECKPOINTED
    5 STATE_CHECKPOINTED

OK: exactly-once side effect preserved across resume.
```

The LangGraph adapter starts the run with `Origin.DETERMINISTIC` provenance, so the
recovery engine returns `resume / safe=True` (no human needed), matching the
deterministic-orchestrator design. This closes the last open item from the earlier
real-LLM section: all three framework adapters (LangChain, LangGraph, OpenAI) have
now been driven by a live model and shown to preserve exactly-once side effects
across a resume when a stable `key` is used.

### LangChain adapter: real-LLM crash-and-resume proof (2026-08-15)

The three soft-resume runs above prove exactly-once across a second *clean*
invocation, but they never proved the harder contract: a hard crash between
`intercept_action` (claim) and `complete` must leave the side effect **uncertain**
and refuse to resume, rather than letting the agent silently re-fire it.
`examples/langchain_real_llm_crash.py` drives a live `gpt-4o-mini` through exactly
that, using two processes that share one SQLite file:

- `crash` mode: the agent is asked to notify the customer. The wrapped tool performs
  the real side effect (appends the order id to an outbox file) and then hard-exits
  the process with `os._exit(137)` (no cleanup), before the ledger records
  completion. The process dies with an open `ACTION_RECORDED` at `started`.
- `resume` mode: a brand-new process opens the same database and asks CONTINUUM to
  `assess` the run.

Live result (model `openai/gpt-4o-mini`, OpenRouter):

```
== Resume mode: a fresh process assesses the crashed run ==
mode: request_human
safe: False
next_allowed_action: reconcile_action:action_f357be53d49eaa19b9fbf739745f887f
uncertain_actions: 1
  - notify.customer status=started
outbox entries (must be exactly 1): 1
   notified Your order O-9 has been processed successfully. ...
event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
OK: crash left an uncertain side effect; resume blocked, outbox not duplicated.
```

This is the exact behaviour the recovery engine promises on a mid-side-effect kill:
the action is flagged `started` (never completed), the run is `unsafe`, and the only
allowed next step is to reconcile the uncertain action by hand. Crucially, the
outbox still contains exactly one entry, so the side effect that did happen during
the crash is not duplicated when a human later reconciles.

### OpenAI Agents SDK adapter: real-LLM crash-and-resume proof (2026-08-15)

`examples/openai_real_llm_crash.py` repeats the same hard-crash contract for the
OpenAI Agents SDK adapter, with the same live-model setup as
`examples/openai_real_llm.py` (an `AsyncOpenAI` client pointed at OpenRouter, the
model wrapped in `OpenAIChatCompletionsModel` over the chat completions endpoint).
The `notify` function-tool appends to the outbox and then `os._exit(137)`; a second
process runs `RecoveryEngine.assess`. Live result (model `openai/gpt-4o-mini`):

```
crash exit: 137
mode: request_human
safe: False
next_allowed_action: reconcile_action:action_693830c3b03892fae9270a3e8a7f80b0
uncertain_actions: 1
  - notify.customer status=started
outbox entries (must be exactly 1): 1
   notified O-9
event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
OK: crash left an uncertain side effect; resume blocked, outbox not duplicated.
```

The OpenAI adapter records state with `Origin.EXTERNAL_AGENT`, so the recovery verdict
is `request_human` (a self-certifying agent must not approve its own unverified work),
which is the expected and safe behaviour. The point proven: the OpenAI adapter
intercepts a live model's tool call through the ledger, the hard kill leaves the
action `started`, and resume is refused until reconciliation.

### LangGraph adapter: real-LLM crash-and-resume proof (2026-08-15)

`examples/langgraph_real_llm_crash.py` repeats the hard-crash contract for the
LangGraph adapter, with the same live-model setup as `examples/langgraph_real_llm.py`
(`create_agent` over `ChatOpenAI` at the OpenRouter base URL). The wrapped `notify`
tool appends to the outbox and then `os._exit(137)`; a second process runs
`RecoveryEngine.assess`. Live result (model `openai/gpt-4o-mini`):

```
crash exit: 137
mode: request_human
safe: False
next_allowed_action: reconcile_action:action_9cdae0d82d9f498df28416a6d6a9888f
uncertain_actions: 1
  - notify.customer status=started
outbox entries (must be exactly 1): 1
   notified Your order O-9 has been processed successfully. ...
event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
OK: crash left an uncertain side effect; resume blocked, outbox not duplicated.
```

The LangGraph adapter starts the run with `Origin.DETERMINISTIC` provenance, yet an
incomplete action still forces `request_human` / `safe=False`: a side effect that may
or may not have completed can never be resumed blindly, regardless of origin. This
confirms the crash contract holds across all three framework adapters when driven by
a live model. The full crash-after-checkpoint / resume matrix is now exercised by a
live LLM for the LangChain, OpenAI Agents SDK, and LangGraph adapters.

### Multi-step live demo: a real LLM orchestrating through CONTINUUM (2026-08-15)

`examples/multitool_real_llm.py` is the closest thing to a real product flow. A live
`gpt-4o-mini` is handed one task, "a customer with order O-9 reports their package
hasn't arrived", and must *orchestrate* three tools through the LangGraph adapter:
`lookup_order` (read-only), `notify_customer`, and `create_ticket` (both side
effects). Each side-effecting tool is wrapped with a **fixed** idempotency key
(`notify:O-9`, `ticket:O-9`) and a checkpoint is written after every tool result.
The agent runs twice over the same run (first pass plus a resume) and the harness
prints the recovery decision and the full event log.

Live result (model `openai/gpt-4o-mini`, OpenRouter):

```
== First pass (model: openai/gpt-4o-mini) ==
agent: I have looked up order O-9 ... the customer has been notified ... a support ticket has been created.
recovery after pass 1: resume safe= True

== Resume: same run, second pass ==
agent: Everything has been handled for order O-9. ...
recovery after pass 2: resume safe= True

notify side effects (must be 1): 1
ticket side effects (must be 1): 1

event log:
    1 RUN_STARTED
    2 ACTION_RECORDED
    ...  (lookup, notify, ticket, plus the model's drifted re-calls)
    7 ACTION_RECORDED
    8..13 STATE_CHECKPOINTED

OK: multi-step live agent handled smoothly; exactly-once side effects preserved.
```

What this proves about "smooth handling": the model freely drifts its argument text
(it rendered `order_id` as both `"O-9"` and `"Order O-9: Customer reports that the
package hasn't arrived. Investigating the late shipment."` within one pass), yet the
**fixed** key collapsed every variant to exactly one recorded side effect. That is
the central real-LLM lesson, confirmed live: an idempotency key derived from the
model's rendered arguments (`key_fn=lambda **kw: f"ticket:{kw['order_id']}"`) would
*not* dedupe this drift and produced a duplicate ticket on the first attempt; only a
stable business key does. The recovery engine returned `resume / safe=True` for both
passes, so the architecture absorbed the model's messiness without human involvement.

## Unresolved

`demo_report.md` (an untracked artifact from the third-party testing above)
changed from 4,997 bytes with five sections to 746 bytes with one, between two
examinations during a single working session. Nothing in this repository
accounts for it.

At the time, `claude` and `gemini` CLI processes were running on other TTYs,
and two `kilo serve` processes had been running all day. **Concurrent agent
sessions are the most plausible explanation, but this was inferred from process
listings and timestamps, it was never confirmed.** It is recorded here as an
open question rather than a closed one.

A related, confirmed observation: files in this repository were modified during
this work by processes other than the session doing the work, including an
`adapters/` package and a `recovery/engine.py` branch that appeared
mid-session. If state seems to change without explanation, check for other
agent processes before assuming a bug.

---

## Repository housekeeping

The commit history on `main` is dominated by website and logo iteration: roughly 50 of ~115 commits are site, favicon, or logo experiments, including one revert (`ea583ec`). This is cosmetic churn, not open code debt. Not tracked as a GitHub issue: rewriting history on a repository others may have forked is disruptive, and a public flame-on-commits is low value. If a clean history matters for the v0.1.0 presentation, squash or rewrite those commits before release; otherwise leave them.

---

## Untracked files, deliberately excluded

- `.mcp.json`, Claude Code registration; hard-codes machine-specific absolute
  paths.
- `demo_report.md`, artifact from third-party client testing.
- `kilo.jsonc`, Kilo's own MCP config, written by Kilo.

---

## Security Extension (in progress, not part of v0.1.0 launch)

Two additive extensions are being prototyped on top of the existing
recovery/checkpoint substrate. Both are additive: they do not change resume,
replay, or the existing crash-time revalidation path. Deliberately scoped to
avoid scope creep (no adversarial training, no new policy language).

- `docs/PROBLEM.md`, the problem statement each extension addresses, with the
  paper, the date, the unmet claim, and our honest "does not solve" framing.
- `src/continuum/security/provenance.py`, `ObservationProvenance` and
  `PlanBranch` (frozen pydantic v2, matching `models.py` conventions).
- `src/continuum/security/trust_gate.py`, `verify_observation` (two-signal
  trust: `verified` / `unverified` / `contested`), `record_observation`,
  `resolve_branch` (risk-tiered escalation to `REQUIRES_REVIEW` for high-risk
  unverified/contested, and contested environment observations), `ReviewGate`.
- `src/continuum/security/revalidation.py`, `RevalidationTrigger`,
  `RevalidationPolicy`, `RevalidationResult`, `maybe_revalidate` (fires on a
  step interval and on app switch, reusing `RecoveryEngine.assess`).
- `src/continuum/security/prompts/secure_planning.md`, the planner prompt
  contract for Extension 1.
- `docs/RESULTS.md`, numbers; the mini-benchmark is still PENDING (runs after
  the core mechanism is proven).

Tests: `tests/test_trust_gate.py`, `tests/test_revalidation_schedule.py`,
`tests/test_toy_task_banner_attack.py` (a cookie-consent banner before/after
pair). All 900 tests pass; `ruff` and `mypy --strict` are clean.

What this does NOT claim: Extension 1 does not defeat an optimized pixel-patch
attack (still open per CaMeLs), it adds an audit trail and escalation.
Extension 2 does not improve long-horizon reasoning, it re-checks ground truth
on a schedule. Neither claims to have "solved" its source paper.

---

## Contributor PR wave and triage (2026-08-19 to 2026-08-20)

A batch of contributor PRs landed on `main` and was merged through review. Each
was verified by checking out the PR branch, running the affected tests in
isolation (so unrelated local work was never disturbed), `ruff check`, `ruff
format --check`, and `mypy`. The only mypy output was the pre-existing
`langchain`/`langgraph` import-not-found noise, which is unrelated to any of
these changes. All merges were squash merges via `gh pr merge --admin` because
branch protection blocks a non-admin merge.

### Merged

| PR | Title | Author | Closes | Verification |
|----|-------|--------|--------|-------------|
| #89 | `fix(benchmark)`: close SQLiteStorage handles so the benchmark runs on Windows | @abyyxhek | #81 | 835 passed, 4 skipped |
| #90 | `fix(mcp)`: make a failed cold start leak free and diagnosable | @Adhi1-2 | #87 | 838 passed |
| #92 | `fix(serve)`: let a sidecar client resume without a memorized id or task file | @abyyxhek | #91 | 844 passed |
| #93 | `fix(mcp)`: report a missing `mcp` extra instead of an import traceback | @Adhi1-2 | #87 | 840 passed |
| #96 | `docs(changelog)`: correct the serve resume test count to six | @abyyxhek | (docs) | changelog only |
| #97 | `fix`: report the failing storage path unescaped so it is copy-pasteable | @Adhi1-2 | #94 | 864 passed |
| #99 | `fix(serve)`: delete the dead auth gate and pin the policy the sidecar really has | @Adhi1-2 | #95 | tests/test_serve.py 32 passed, ruff clean |

### Closed without merge

- #98 (`fix(cli,mcp)`: stop escaping the path in the cannot-open-storage error,
  @abyyxhek) was closed as a duplicate of #97. #97 already fixed the same two
  sites with the same literal-quote approach; #98 was filed after #97 and added
  nothing new, so it was labelled `duplicate` and closed.

### Maintainer changes landed alongside

- `0cef651` `fix(mcp)`: register the server as `continuum-mcp` so it is found at
  cold start (#87). This was the root cause of the MCP cold-start failure: the
  config key and the advertised `MCPServer` name said `continuum`, while the
  console script, docs and `CLAUDE.md` all say `continuum-mcp`. Both the
  `.mcp.json` key and the `MCPServer(name=...)` were changed to `continuum-mcp`.
- `8db7745` `docs`: add Abishek to the contributors list and ship circular
  contributor avatars under `docs/contributors/`.

### Issues resolved this session

- #81 (benchmark crashes on Windows from unclosed handles): fixed by #89.
- #87 (MCP server reports `ready:false` at session start): addressed on three
  fronts. The name mismatch by the `continuum-mcp` change above, the leaked
  handle plus traceback-on-cold-start by #90, and the missing-`mcp`-extra
  import death by #93.
- #91 (sidecar `resume` drifted from the MCP tool it mirrors): fixed by #92,
  which also added six serve tests that diff the sidecar payload against the live
  `continuum_resume` so the two surfaces cannot silently diverge again.
- #94 (cannot-open-storage message escaped backslashes, so a Windows path was not
  copy-pasteable): fixed by #97. Reported with a full diagnosis by @abyyxhek,
  including that the escaping broke the exact guarantee #87 was meant to provide
  and that it had already shipped a Windows-only breakage once (#81 was the
  first).
- #95 (the `serve` sidecar exported a `MUTATING` constant describing an auth
  policy it did not implement, and no test pinned the real one): fixed by #99.
  Also reported by @abyyxhek. The PR deletes the dead `_auth_check`, keeps
  `MUTATING` as descriptive metadata with a docstring stating it does not govern
  auth, corrects `SidecarAuth`, and adds twelve regression cases over
  `list_methods()`.

### Who solved what

- @abyyxhek: authored and merged #89, #92, #96. Reported #91 and #95. Produced
  the full root-cause diagnosis for #87 (the `continuum` vs `continuum-mcp` name
  mismatch) and for #94 (both error sites, the POSIX/Windows asymmetry, and why
  the existing MCP test missed it on a clean checkout). #98 was his duplicate of
  #97.
- @Adhi1-2: authored and merged #90, #93, #97, and #99. Each paired the fix with
  tests and a CHANGELOG entry. Solved the three remaining #87 sub-defects (leak,
  traceback, missing extra) plus both #94 and #95.

Net: every issue that surfaced during this window is closed. The open items
remaining are the design and research briefs (#82 to #88) and the older roadmap
issues (#10 to #13, #6), none of which were part of this batch.

### Local feature work committed during this window

While the PRs above were being reviewed, a set of larger local changes was
committed to `main` and pushed rather than left untracked. They are recorded
here so the tree state is self-explanatory. The CHANGELOG `[Unreleased]` `Added`
section already describes each in detail.

- `3c20966` `feat(interchange)`: portable recovery-state interchange schema (B4).
  `continuum.interchange` turns durable output into a versioned JSON envelope.
- `7678ee5` `feat(storage)`: forward schema migration framework (B2.1).
- `deced07` `feat(concurrency)`: lease and distributed-lock coordinator (B2.2).
- `1a81c67` `feat(storage)`: PostgreSQL backend and URL routing (B2.3).
- `fc9dfa1` `docs(changelog)`: record B4, B2.1, B2.2, B2.3 in Unreleased.
- `4908115` `test(cli)`: cover PostgreSQL URL routing and clean failure (B2.3).
- `c00c8eb` `docs(audit)`: MCP surface audit and auto-resume integration notes.
- `3d37714` `chore(bugaudit)`: diagnostic scripts used during the MCP bug audit.
- `24dcf67` `build`: add `uv.lock` for reproducible installs.

The PostgreSQL backend is unverified against a live server in this environment
(no `CONTINUUM_TEST_POSTGRES_DSN`, no `psycopg`); its tests skip cleanly and it
should be validated in CI before reliance.

### Housekeeping note

A stale stash (`90a6d58`, "wip: continuum-mcp name fix + changelog") remains in
the reflog. It duplicates work already committed in `0cef651`/`dc400f5` (the
`continuum-mcp` rename and the e2e-autonomy-test restore), so it can be dropped
with `git stash drop` after a glance. It is harmless where it sits.

---

## 2026-08-24: months-scale synthesis and filed issues

Live arXiv sweep on 2026-08-24 plus review of all 9 research notes produced a consolidated synthesis for running agents for weeks and months:

* New doc: `docs/research/WEB_SYNTHESIS.md` links every claim. Live pulls: HORIZON 2604.11978 (3100 trajectories, subplanning dominates), AgentRewind 2608.14380 (env rewind is top ablation, MettleBench), ACRFence 2603.20625v1 (10 of 10 duplicate commits, Action Replay and Authority Resurrection), Weighted Memory Tree 2608.20631 (retention-scored hierarchy), Beyond Suspicious Steps 2608.17718 (RGE trust), MileGPO 2608.19803, FM-Bench 2608.18423 (20 years, 340 to 400 decisions).
* Maps onto existing coverage and the 4 open gaps from `docs/research/long_horizon_gaps.md` (curated resume context, structured attempt memory, milestone-anchored plan, env rewind alignment via issue 292) plus the three tax notes (`instant_detection.md`, `confirm_tax.md`, `token_floor.md`).
* Six additive novelty layers ordered by falsifiable tests: Layer 1 PLAN_UPSERT milestone plan, Layer 2 structured AttemptLesson, Layer 3 instant detection with scoped confirm plus token floor, Layer 4 dual-state rewind, Layer 5 sleep-time consolidation, Layer 6 prefix trust monitor (advisory).
* Two professional feature requests filed with the `feature_request.yml` template, no em dashes, TensorFlow-level detail:
  * #312 `feat(state): durable structured plan via PLAN_UPSERT for long-horizon recovery` (Layer 1)
  * #313 `feat(recovery): structured attempt memory with falsification lessons for cross-session resume` (Layer 2)
  Next session should continue with Layer 3 (hook plus scoped confirm) and extend `benchmark/phase6` toward HORIZON judge and FM-Bench horizon for the metrics listed in `ARCHITECTURE_EVOLUTION.md` section 9 (unsafe resume rate 0, recovery decision accuracy, repair precision, duplicate effects).

---

## Codebase snapshot (2026-08-20)

Captured while preparing the README project-structure section. The tree is 204
tracked files, 118 Python files, about 30,300 LOC total. The core library
`src/continuum` is 60 files / about 14,800 LOC; the test suite is 45 files and
about 900 tests collected.

Layers, by size:

- Mature and heavily tested (the bulk, about 72% of core): `storage/`,
  `state/`, `adapters/`, `mcp/`, `cli/`, `actions/`, `checkpoint/`,
  `recovery/`, `events.py`, `models.py`.
- Committed but newer or not yet fully vetted:
  - `security/` (provenance, trust gate, revalidation) is marked not part of
    v0.1.0; `docs/RESULTS.md` mini-benchmark is still pending.
  - `storage/postgres.py`, `storage/migrations.py`, and `concurrency/` (lease
    coordinator) are the B2.x work. The Postgres backend is unverified against a
    live server in this environment (no `CONTINUUM_TEST_POSTGRES_DSN`, no
    `psycopg`); its tests skip cleanly.
  - `interchange/` (B4 portable envelope) is done and tested.
- `benchmark/`, `environment/`, `plugins/`, `observability.py`, and `serve/`
  round out the surface.

Three entry points from `main`: the `continuum` CLI (`cli/main.py`), the
`continuum-mcp` server (`mcp/server.py`), and the `continuum serve` sidecar
(`serve/server.py`). The full module map is in the README Architecture section
and [references/architecture.md](references/architecture.md).

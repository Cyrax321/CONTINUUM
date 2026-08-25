# Worklog

A chronological record of what was done and why, for whoever picks this up
next. It complements `STATUS.md` (the state snapshot and verification record)
and `CHANGELOG.md` (user-facing changes). Where this document summarises a
session, `STATUS.md` carries the detail and the evidence.

## Session 1: v0.1.0 core and first release (2026-08-11)

Built CONTINUUM v0.1.0 from the plan in `project.md`.

- Phase 1 core: event log, deterministic semantic state fold, immutable state
  versioning, checkpoint diffing.
- Phase 2: SQLite transactional event store (WAL, `synchronous=FULL`,
  IMMEDIATE transactions, hash-chained append-only log), semantic checkpoint
  manager with adaptive persistence policies, invariant state validator.
- Interactive website and live fault simulator moved into `docs/`, GitHub
  Pages deploy workflow added.

First release commit: `ee9032b`.

Most of the 2026-08-11 history after that is website and logo iteration
(roughly 50 of ~115 commits at the time). One revert (`ea583ec`) undid a
stylesheet split. Noted in `STATUS.md` under Repository housekeeping.

## Session 2: CLI, action ledger, adapters (2026-08-11)

- Phase 8 CLI: 14 stdlib-argparse commands with TTY-aware colour, `NO_COLOR`
  support, and the exit-code contract (only a verified-safe run exits `0`).
- `feat(actions)`: idempotent action ledger with claim/complete, raising
  `UnknownSideEffect` rather than guessing an unknown outcome.
- `feat(adapters)`: generic `AgentAdapter` facade, then LangGraph and OpenAI
  Agents SDK adapters, written against each framework's actual API surface.
- `feat(examples)`: context compaction and model switch demos (Phase 9).

## Session 3: security and authorization (2026-08-11)

- `9738b9e` carried event provenance so agents cannot self-certify state:
  `Event.source` is captured at write time and signed; the projector propagates
  it; the validator marks self-certified components `REQUIRES_REVIEW`;
  MCP-written state is tagged `Origin.EXTERNAL_AGENT`. Required a strict v1 to
  v2 schema migration; the database was reset.
- `103b83c` gated mutating MCP tools behind a caller allowlist, deny by default.
  `CONTINUUM_MCP_MUTATING_CLIENTS` accepted as an alias for the older
  `CONTINUUM_MCP_ALLOW` name (`05770b4`).
- PR #3 (an independent attempt at the same authorization fix) was reviewed
  and closed without merging because it failed open on two paths. The
  `CONTINUUM_MCP_MUTATING_CLIENTS` name was kept from it. Full write-up in
  `STATUS.md`.

## Session 4: MCP Inspector verification (2026-08-12)

Verified the server end to end through `@modelcontextprotocol/inspector`
v2.1.0 in `--cli` mode, driving the real stdio protocol boundary. Three
sequences (clean crash, crash between intercept and complete, trusted-writer
state) confirmed the two-phase interception, uncertainty handling, and
authorization behave correctly under real process deaths. Scripted, not
autonomous. Detail and JSON in `STATUS.md`.

## Session 5: code audit and audit-driven fixes (2026-08-12)

A module-by-module audit filed seven issues and produced four fixes, each with
regression tests:

- `91aee41` rejects over-total progress before it is written (issue #15).
- `71c86b3` stops read-only `list_actions` from backfilling `RUN_STARTED`
  (issue #20).
- `e9c5f78` protects the never-dropped recovery-context sections by identity,
  not sorted position (issue #16).
- `1bcc933` makes `continuum events` honour the not-found exit code (issue
  #18).

CI was migrated to Node 24-compatible GitHub Actions (`d8f80dd`). Adapter
lint, format, and mypy issues were resolved (`6fb91a0` through `89307ff`).
`examples/` were made lint-clean and added to CI coverage (issue #8).
CONTRIBUTING clone URL, PyPI homepage, license copyright, and a publishing /
premium roadmap plan were fixed or added.

## Session 6: CONTINUUM-Bench (2026-08-12)

`feat: add CONTINUUM-Bench minimal recovery benchmark harness` (`0bebb61`):
`continuum benchmark` runs three scenarios across three strategies and prints
measured numbers. Phase 12 shipped in minimal form; the fuller suite, published
baselines, and dashboard remain a goal.

## Session 7: Claude Code MCP verification and orphaned-WAL fix (2026-08-13)

- Registered the server in Claude Code and drove it end to end from a real
  session. All 9 tools callable, authorization boundary intact, MCP-written
  state resumes as `request_human` / `safe: false`.
- Hit `Failed to connect`: a previously hard-killed server had left orphaned
  `<db>-wal` / `<db>-shm` sidecars, and the next open failed at
  `PRAGMA journal_mode=WAL` with `disk I/O error`.
- `8ef54c9` self-heals at startup: `_open_server_storage` clears orphaned
  sidecars and retries the open once, re-raising when there was nothing to
  remove. Two regression tests.
- `b8a055c` added `e2e-autonomy-test/`: a three-script harness to answer the
  still-open question of whether an unscripted LLM agent would use CONTINUUM
  correctly on its own.

## Session 8: the issue #6 e2e series (2026-08-13)

Three full runs against real Claude Code sessions, each hard-killed mid-batch
and resumed in a brand-new session. All scored 7/7 on the mechanics checks and
demonstrated real autonomy: agents used `record_progress`, routed sends through
intercept/write/complete, called `resume` before acting, surfaced the
`request_human` verdict, and refused to re-send verified-sent invoices.

The runs exposed a dedup defect: `continuum_intercept_action` hashed the
caller's raw arguments, so relative vs absolute path formatting produced
different idempotency keys and the resumed session was told `proceed: true`
for already-sent invoices. Fixed in `1fc97cf` by adding a stable `key`
argument (e.g. `invoice:INV-001`) that makes two attempts the same action
regardless of argument shape. Regression test mirrors the exact failure.

Also recorded: ledger pollution from the `fail_action(certain=true)`
workaround (gone with the fix), and an open observation that
`continuum_resume` reported `checkpoint_version: 0` despite session 1 taking a
checkpoint. Full detail in `STATUS.md` under "The issue #6 e2e series".

## Session 9: ledger performance profiling (2026-08-13)

Measured the suspect lag seen when resuming after 4-5 actions. Findings:

- Ledger replay is O(n) per call, O(n^2) over a run: 10 actions at 1.5 ms per
  call, 200 actions at 23.4 ms per call (ratio 100 to 200: 2.06x).
- MCP server-side calls are fast at e2e scale: `list_actions` 4.7 ms, `resume`
  0.7 ms at 5 actions. Event append including fsync about 0.15 ms.
- Conclusion: the perceived lag is dominated by LLM round trips per tool call
  (seconds-scale), not the database. Fewer tool calls is the lever, and the
  dedup fix is the change that removes the spurious call cycle. The O(n^2)
  replay is real but negligible below hundreds of actions; a replay cache is
  deferred until a run grows that large.

## Session 10: defensive dedup hardening (2026-08-13)

Re-read the three e2e transcripts and found the real drift was argument field
renames (`target` / `outbox_file` / `outfile` / `file`), action type drift in
one run (`send_invoice` vs `send-invoice-email`), and `external_id` shape
drift. The only stable identity was the resource token (`INV-001`), surviving
as scalar value, path basename, and external id stem. The stable-key fix could
not help when the caller supplied no key or renamed fields.

Added two defensive layers:

1. `arguments_hash` / `idempotency_key` now canonically normalize path-like
   arguments (lexical `normpath` plus `~` expansion; URLs untouched) before
   hashing.
2. `ActionLedger.claim()` gains a token-based identity fallback for the
   no-explicit-key case: shared identity tokens (scalar values, path
   basenames/stems, external ids; weak tokens and the `continuum_run_id`
   plumbing token excluded) recognize a unique same-type match. Completed match
   returns `fresh=False` with the stored result and real recorded key;
   interrupted match surfaces as uncertain; ambiguity falls through.

Caught during testing: the fallback initially deduplicated distinct tool calls
because `continuum_run_id` (a strong token) was present in every claim's
arguments; the plumbing exclusion fixed it and a regression test pins it.

Verification: 700 tests pass, ruff clean, mypy clean on changed files.

## Session 11: repository-wide bug audit (2026-08-14)

Read every behavioural module and exercised it, surfacing 20 evidence-backed
issues (#29 to #49, excluding the externally filed #39). Labelled `good first
issue` or `help wanted` and left as the contributor backlog.

Eight were fixed and merged to `main`, each with regression tests:

- Replay now verifies the stored version instead of only re-deriving it (issue
  #31, PR #50).
- `continuum replay --upto N` rejects prefixes that exclude `RUN_STARTED` with a
  clear message (issue #32, PR #55).
- Progress writer rejects negative `completed` / `failed` even when `total` is
  omitted (issue #38, PR #51).
- `LLMExtractor.extract()` falls back to the deterministic state on a malformed
  proposal instead of raising (issue #40, PR #56).
- `LLMExtractor._merge` collapses ids repeated within a single proposal (issue
  #41, PR #52).
- Cache hit keeps a result dict holding the `__return_value__` envelope key
  intact (issue #44, PR #53).
- Self-certified progress is no longer relabelled `UNKNOWN` by `--tolerate-unknown`
  (issue #48, PR #55).
- Issue #37 closed by the same batch of fixes.

Detail and the still-open nine live in `STATUS.md`; user-facing notes are in
`CHANGELOG.md`.

## Session 12: README contributors and deleted-account fix (2026-08-17)

Added a Contributors section with circular profile avatars for the four active
contributors. While doing so, found that `sharyaropensource` (Sharyar Naseem)
had a deleted GitHub account: profile and avatar both return 404, so GitHub no
longer attributes his commits and his PRs are disassociated. The commits
themselves remain on `main`, so the work is preserved in history.

Kept his attribution as plain text and removed the dead profile and avatar links
so the README ships no broken reference. The other three contributors
(`Cyrax321`, `dchaudhari7177`, `lesbass`) resolve normally.

## Session 13: MCP caller authentication (issue #1) (2026-08-19)

Closed the last open trust bug. Authorization (added in `d9365c8`) denied
mutating tools by default, but `clientInfo` was client-asserted and never
verified, so a hostile local process could impersonate an authorized caller.

Added an optional, fail-closed shared-secret authentication:

- `AuthPolicy` and `load_auth` in `src/continuum/mcp/authz.py`. When
  `CONTINUUM_MCP_TOKEN` is set, the server requires the caller to present that
  secret in the `initialize` handshake's `_meta.authToken`. The check is
  fail-closed: a missing, empty, or mismatched secret always refuses, and an
  empty configured secret refuses rather than opening the door. The closed PR
  #3 failed open on a `ValueError`; `test_auth_fails_closed_when_required_but_unset`
  pins the opposite behavior. An unset token leaves authentication disabled, so
  the default local, single-user, no-account behavior is unchanged.
- Wired into the tool `guard` in `src/continuum/mcp/server.py`: authenticate
  before authorize, both must pass, denial precedes any write.
- `token_from` reads the secret from `context.session.client_params.meta`.
- `tests/mcp_helpers.py` `fake_context` now carries an `auth_token` for the
  handshake `_meta`.
- Tests in `tests/test_mcp_authz.py`: policy unit tests, the fail-closed case,
  per-client tokens, env resolution, and server-level enforcement (right
  secret + name succeeds; wrong/missing secret refused; a stranger with the
  secret is still unauthorized; read-only tools unaffected).

Also corrected the priority plan in `project.md`: #17 and #19 were already
resolved (commits `82b9f1c`, `f145818`) and were wrongly listed as open; #1 is
now the only remaining trust bug and is closed by this session.

Verification: `tests/test_mcp_authz.py` and `tests/test_mcp_server.py` pass;
`ruff` and `mypy --strict` clean on the changed files.

## Session 14: plugin Registry and capability seams (B1) (2026-08-19)

Started the "attach to any system" work from `references/integration-architecture.md`
section 3 (the Cordis-style plugin seam). Built the Tier 1 foundation:

- `src/continuum/plugins/registry.py`: a dependency-injected `Registry` mapping
  names to services, resolved by type, with reversible `Registration` handles
  (unregister) so a plugin can tear down what it contributed. `all_of(type_)`
  returns every registered service conforming to a seam.
- `src/continuum/plugins/seams.py`: the four capability seams as
  `runtime_checkable` `Protocol`s, `EnvironmentProvider` (re-exported from
  `continuum.environment`), `StateExtractor`, `ActionReconciler`, `ValidationRule`,
  plus a `Reconciliation` dataclass for reconciler output.
- `src/continuum/plugins/__init__.py`: single import surface.
- `GitProvider` in `src/continuum/environment/snapshot.py`: the first
  *discoverable* environment provider. It reads `git rev-parse HEAD` for a path
  instead of trusting a declared version, and never raises (reports
  `UNKNOWN_VERSION` outside a repo or on failure). Exported from the environment
  package.

`EnvironmentProvider` already existed as an ABC (`StaticProvider`,
`ValueProvider`, `FileProvider`, `CallableProvider`), so the seam was real before
this; B1 adds the registry, the three other seams, and a discoverable provider.

Tests: `tests/test_plugins.py` covers the registry (register/resolve/reverse/
filter), seam conformance (dummy implementations satisfy `isinstance` and return
correct types), built-in providers conforming, and `GitProvider` reading HEAD in
a temp repo and reporting UNKNOWN outside one.

Verification: `tests/test_plugins.py` (12) and `tests/test_environment.py` (25)
pass; `ruff` and `mypy --strict` clean on the changed files.

## Open items carried forward

- Issue #17: resolved (`82b9f1c`); older-schema databases now refused at open.
- Issue #19: resolved (`f145818`); `resume --repair` now records the plan.
- Issue #1: resolved (Session 13); optional shared-secret authentication.
- Orphaned `demo_report.md` size change and concurrent-agent edits (inferred,
  never confirmed).
- `checkpoint_version: 0` on resume despite a session-1 checkpoint.
- Stale editable metadata in `pip show continuum-agent` (cosmetic).
- Re-running the e2e kit after the dedup fix to confirm the positive path
  (`proceed: false` on resume without agent workaround) before closing issue
  #6.
## Session 15, 2026-08-19 (continuum serve sidecar, B0)

Goal: deliver the Tier 0 attachability boundary from
references/integration-architecture.md. Any external process, in any language,
can drive CONTINUUM's recovery operations without embedding Python or the `mcp`
SDK.

What shipped:
- `src/continuum/serve/server.py`: `SidecarServer` (newline-delimited JSON over
  stdio), `SidecarAuth` (fail-closed shared secret via `CONTINUUM_SERVE_TOKEN`,
  modeled on the MCP `AuthPolicy`), `MalformedRunLog`, `SidecarError`,
  `MethodNotFound`, `NotAuthorized`, `BadParams`. The 10 handlers mirror the MCP
  tool surface: `record_progress`, `checkpoint`, `validate`, `resume`, `confirm`,
  `intercept_action`, `complete_action`, `fail_action`, `reconcile_action`,
  `list_actions`. The server imports ONLY core modules (never `continuum.mcp`),
  so `continuum serve` works without the `mcp` extra.
- `src/continuum/serve/__init__.py`: reference `SidecarClient`, `SubprocessClient`,
  `serve_subprocess()` (launches a real `continuum serve` child and returns a
  connected client), `run_serve()`, and `cmd_serve` (wired into
  `src/continuum/cli/main.py`, added to the storage-bypass tuple alongside
  `benchmark`/`attest-keygen`).
- `tests/test_serve.py`: dispatch unit tests, stdio-loop parsing, fail-closed
  auth (no token required by default; refuses when a token is configured and
  absent/present-but-wrong), and a real subprocess end-to-end path.

Notable decisions / discoveries:
- `continuum.mcp.__init__` imports `server`, which imports `mcp.server`, so any
  `continuum.mcp.*` import drags in the `mcp` SDK. The sidecar deliberately stays
  core-only for this reason.
- `RunNotFound` lives in `continuum.storage`, not `continuum.models`.
- `resume` over a self-certified (agent/MCP-reported) run returns `request_human`
  and `safe=False` until a human confirms, exactly like the MCP server. The
  sidecar's `resume` handler reflects that, so external clients see the same
  contract as MCP clients.

Verification: `tests/test_serve.py` (11), full suite, `ruff`, and `mypy --strict`
all pass. Documented in CHANGELOG.md (Unreleased, Added) and project.md (B0).

Open follow-ups (not done here): multi-language SDKs and an HTTP/gRPC transport
are still future work; the protocol is transport-pluggable but only stdio ships.

## Session 16, 2026-08-19 (A2 proof: CONTINUUM-Bench proves issue #6)

Context: A2's observability half (metrics collector, Phase 14 recovery
dashboard, `--dashboard` flag) already merged via PR #60. The remaining half was
the minimal CONTINUUM-Bench that proves issue #6 (idempotency under argument
drift).

What shipped:
- `src/continuum/benchmark/__init__.py`: `IdempotencyResult`,
  `run_idempotency_benchmark(total=50)`, `_try_idem_action`, and
  `render_idempotency`. The scenario drives the real `ActionLedger` (the same
  path the LangGraph/OpenAI/MCP adapters call) with an agent that re-attempts
  each of N external actions twice using a different path shape (absolute vs
  relative path). Four methods: `continuum_key` (stable key), `continuum_drift`
  (no key, relies on drift recognition), `naive_retry`, `replay`.
- `src/continuum/cli/main.py` `cmd_benchmark` now runs both suites (recovery +
  idempotency) and prints/JSON-dumps both.
- `tests/test_benchmark.py`: asserts CONTINUUM recovers with 0 duplicate work and
  detects stale env, and that the #6 scenario yields 0 duplicate side effects
  for `continuum_key`/`continuum_drift` vs N for the baselines.

Real numbers (run 2026-08-19, total=200 recovery / 50 idempotency):
  continuum_key        50 actions, 50 attempts, 50 distinct, 0 dups
  continuum_drift      50 actions, 50 attempts, 50 distinct, 0 dups
  naive_retry          50 actions, 100 attempts, 50 distinct, 50 dups
  replay               50 actions, 100 attempts, 50 distinct, 50 dups

Verification: `tests/test_benchmark.py` (3) plus full suite, `ruff`, and
`mypy --strict` clean. Documented in CHANGELOG.md (Unreleased, Added),
STATUS.md (benchmark + #6 proof), and project.md (A2 marked done).

A2 is now complete. Next in plan order: B2 (PostgreSQL, centralized server,
distributed locking, schema migration).

## Session 17, 2026-08-19 (C1: fix the open correctness backlog)

Context: the session resumed on an uncommitted working tree carrying draft fixes
for the C1 triage issues (#29, #30, #33, #34, #36, #42, #43, #45). Three tests
were red, `idempotency.py` was unformatted, and the draft had two real problems
of its own, both found by running the code rather than reading it.

What the draft got wrong (and how it was fixed):
- **A silent-data-loss regression.** Widening `_is_strong_token` so plain words
  count as identity (correct for #33) combined with `_identity_match`'s
  single-shared-token rule to collapse genuinely distinct actions: two
  `ticket.create` calls differing only in title matched on the shared value
  `urgent`, so the second ticket was reported already-done and never created.
  Verified against HEAD to confirm the regression was new. Fixed by requiring
  *containment* of one token set in the other rather than intersection.
- **Containment alone broke the issue #6 benchmark.** Absolute-vs-relative path
  drift gives each side a token the other lacks (`/data/invoices/INV-5.pdf` vs
  `invoices/INV-5.pdf`), so `continuum_drift` went from 0 to 50 duplicate side
  effects. Fixed with `leaf_tokens`: compare identity at the basename, dropping
  the container path that a re-rendering changes. Only the benchmark caught this
  (the unit suite passed), so the case now has its own test.
- **#36 was not actually fixed.** The issue's own repro passes an `int`
  (`{'row_id': 4821}`), and `identity_tokens.collect` only handled `str`, so the
  token set was empty regardless of `_is_strong_token`. Non-bool ints are now
  tokenised.
- **#42's reason text was misleading.** `reason` was keyed on `needs_person`, so
  an interrupted action reported "was escalated for review" though it never was.
  `reason` now follows status; `requires_human` follows status + `strict_unknown`.

Notable decisions / discoveries:
- Identity is now decided at basename level, which is the assumption the basename
  token always encoded: two same-type actions on same-named files in different
  directories are treated as one. Documented in `leaf_tokens`.
- Failing to deduplicate is recoverable (that is what an explicit `key` is for);
  falsely deduplicating silently destroys a side effect. The matcher errs toward
  a new slot whenever it is not confident.
- `continuum history`'s JSON key changed from `versions` to `checkpoints`. No
  other code, test, or doc referenced the old key.
- `_STOPWORDS` matters less under containment but is kept: it drops filler before
  it can distort a comparison, and both it and `_WEAK_TOKENS` are now matched
  case-insensitively.

Verification: 803 passed, 4 skipped; `ruff check` and `mypy src/continuum` clean.
Every one of the nine new/updated tests was confirmed to fail with `src/` reverted
to HEAD, so each proves its issue rather than merely passing.

Not done here: `src/continuum/serve/server.py` and `tests/test_serve.py` are
unformatted on `main` (pre-existing, from `4605c00`); left alone to keep this
change reviewable. Issue #34 was a documentation fix only: `scoped_to_run=False`
still cannot enforce cross-run uniqueness, since the ledger only replays its own
run; a store-wide lookup remains unimplemented.

## Session: enforced durability sprint (2026-08-22 to 2026-08-24)

The largest working session in the project's history. Closed every issue on
the enforced-durability roadmap (#213), the original durability gap (#207),
and eight supporting issues. Shipped 20+ PRs, all reviewed and merged to
`main`.

### The core insight

CONTINUUM is a verification-and-enforcement plane that does not carry the
agent's mind. Its differentiator is three guarantees nobody else provides:

1. No self-certification: agent-reported state degrades to human review.
2. Side effects require claims: unclaimed effects are blocked at the harness
   boundary, not by convention.
3. Recovery decisions verify against reality before declaring safety.

Everything built this session extends those three guarantees to where agents
actually run.

### What shipped (chronological)

**Observation hooks (#207, PR #210).** `continuum observe` reads one tool-call
payload from stdin and appends a TOOL_COMPLETED event with the file path,
byte count and SHA-256 as it exists right now. `hooks install claude-code`
wires it via PostToolUse. Verified live against real Claude Code sessions:
files written by the agent appear in the event log with correct digests even
after `kill -9`.

**Dashboard bind hardening (#270).** The dashboard bound to all interfaces,
exposing recovery contracts unauthenticated. Default changed to loopback;
operators opt into exposure via `--host 0.0.0.0`.

**Lazy adapter imports (#214).** Importing `continuum.adapters` eagerly
pulled openai (~1.3s) and langgraph (~0.8s) into every process. Optional SDK
adapters now resolve lazily via PEP 562. MCP startup dropped from ~3 s to
~0.1 s.

**Enforcing gate (#217).** Pre-tool-use hook denies side-effect calls without
a live ledger claim. Keys derived from configuration templates against
structured arguments, never from LLM-authored text. Decision table mirrors
the ledger exactly: unclaimed denies with instructions; completed refuses as
duplicate; unknown demands reconciliation.

**Action index (#216).** Schema v3 adds an action_index projection for
cross-run idempotency lookups, replacing O(total events) scans with indexed
reads (~1000x at 300 runs). Postgres parity shipped.

**Reconciler probes (#218).** `.continuum/reconcilers.json` registers one
probe per action type; `continuum reconcile` settles uncertain actions
automatically from external-system checks. Definitive verdicts land as
DETERMINISTIC-sourced ACTION_RECONCILED events.

**Reasoning summaries (#235).** `continuum_record_summary` stores a bounded
self-authored plan summary (4096-char cap); briefing serves it at session
start so resumed sessions inherit plan state instead of guessing from
counters.

**Native LangGraph checkpointer (#236).** CONTINUUM implements
BaseCheckpointSaver over its own storage (schema v4). thread_id maps to
`lg-<thread>` runs; every put lands provenance-tagged STATE_CHECKPOINTED
events into the hash chain.

**Replay-safety guard (#237).** The gate decision table extracted into
`replayguard.evaluate()`, shared by gate, gateway and adapters.
`langgraph_protected_node` wraps graph nodes so interrupt/crash replays
become cache hits. Chaos matrix encoded as executable tests.

**Production server mode (#238).** Live-Postgres CI job running contract
tests against Postgres 16. HTTP transport for `continuum serve`. Gateway
backfill SQL corrected to jsonb operators.

**Event-log compaction (#239).** `continuum compact <run>` archives the
pre-anchor prefix verbatim into events_archive (schema v5). Live chain stays
append-only; verify walks anchored logs natively. Compact auto-creates a
forced checkpoint at current head because anchoring at an ancient version
left nearly everything live.

**Retry budgets (#240).** `.continuum/budgets.json` caps attempts per action
type at claim time. Every claim slot counts; settlements do not.
`intercept_action` refuses beyond-budget claims.

**Version pinning (#241).** Closed-set pinning dict (prompt_sha256,
tool_schema_sha256, model_id, policy_version) stored verbatim on claims and
summaries. Resume diffs caller-supplied pins against newest recorded set.

**HITL dashboard surface (#242).** Run page renders operator buttons for
confirm/reconcile/complete whenever a run is blocked or holds uncertain
actions. Fail-closed until CONTINUUM_DASHBOARD_TOKEN is set.

**Multi-agent parent/child runs (#243).** Schema v6 adds parent_run_id.
Parent resume composes family worst state: uncertain child blocks parent.
`continuum tree` renders hierarchy. A2A task ids ride on metadata.

**Informed retry (#265).** Engine-authored failure summaries injected into
post-recovery resumes via human_steps.

**Fork semantics (#259).** Fork detection at the gate, approve_fork with
lineage events, independently resumable children.

**Semantic replay-or-fork (#291).** Three similarity backends (exact/fuzzy/
embedding) classify post-restore calls as replay/fork/fresh using configurable
thresholds. Fuzzy backend catches LLM paraphrasing without external services.

**Dashboard bind hardening (#270).** Dashboard bound to all interfaces,
exposing contracts unauthenticated. Changed to loopback default;
`--host 0.0.0.0` available for explicit opt-in.

**Windows portability (#211).** External PR #212 merged fixing child-env
inheritance in test helpers.

### Issues closed

#207, #208, #209, #214, #216, #217, #218, #235, #236, #237, #238, #239,
#240, #241, #242, #243

### Issues still open

#215 PyPI (maintainer-blocked), #211 Windows CI runner, #213 umbrella,
#244 novelty umbrella, #254 payload offloading, #258/#259/#265 community
features, #266-#285 contributor good-first-issues, #288 provenance graph
(deferred), #289 authority lifecycle (backlog)

### Final numbers

1348 passing, 24 skipped. Ruff clean, strict mypy clean across ~100 source
files. Five integration seams, six schema migrations, eleven MCP tools,
twenty-five CLI commands. Every feature verified against real Claude Code
sessions with hard kills and live protocol boundaries.

## Session 18, 2026-08-25 (issue #383: degrade instead of die, PR #385)

Fixed #383 in a dedicated worktree (`/tmp/wt-383`, branch
`fix/degrade-unprojectable-fold`, self-assigned before starting). Three
commits, each answering one review round.

**The fix (3f2b769).** `project`/`project_incremental` gained
`on_unprojectable="raise"|"degrade"` (default unchanged). Degrade stops at the
earliest refused event and returns the last-good prefix marked
`SemanticState.status=INVALID` with `unprojectable_at_sequence/_event_type/
_reason`; it never skips past the break, and raises if nothing folds before
it. Opt-ins limited to diagnostic surfaces (engine restore path, CLI
status/inspect/replay, serve progress report and dep dedup, benchmark
readout); the #364 write guard `_project_candidate` and every checkpoint
capture path deliberately stay on raise. Recovery decides REQUEST_HUMAN.
Reproduction used the `_poison` helper from tests/test_cli.py; all four dead
commands confirmed failing before any change.

**Review round 1: the contract must carry the break (0768e38).** Resume's
prose named the break but `required_actions` was empty and `next_allowed`
rendered as "continue" over a requires_human verdict. New `RepairKind.
REPAIR_LOG` step (sorted first) gives the contract real work, qualified
`verified` entries (`goal (through sequence N)`), a `projection (invalid)`
entry in `invalidated`, and honest fallbacks at all three `or 'continue'`
sites (contract render, engine render, dashboard).

**Review round 2: cross-version checkpoint break (39b0756, ddbb6bd).** The
four added fields changed `StateCheckpoint.content()`, so every pre-existing
checkpoint failed verification as tampered, and new bodies carried fields old
readers (`extra="forbid"`) reject. `PROJECTION_BOOKKEEPING` now excludes them
from the digest and from all four persisted-body write sites (sqlite and
postgres, checkpoints and versions). Lesson recorded for next time: no
same-process round-trip can catch a hash-compatibility break, and a naive
strip-and-reread fixture passes against broken code because pydantic
re-injects defaults on reload; the fixture must also re-seal the hash over
the reduced payload, exactly as the old writer did.

Red-before-green proven per round by checking out earlier trees over `src/`
(stash alone is insufficient once changes are committed). Final gates:
1425 passed, 23 skipped, ruff clean, 206 files formatted, strict mypy clean
on 104 files, mcp pinned to 2.1.0 first. All CI checks green on the branch.
Options 2 (repair/amend) and 3 (fork-from-last-good-prefix) remain open by
design; the reviewer floated moving the bookkeeping fields off
`SemanticState` entirely as follow-up scope.

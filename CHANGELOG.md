# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Security Extension (additive).** New `continuum.security` package on the
  existing recovery and checkpoint substrate, without changing resume, replay,
  or the crash-time revalidation path:
  - *Secure Planning Loop* (`provenance.py`, `trust_gate.py`): observations carry
    provenance and are verified by two independent signals (`verified` /
    `unverified` / `contested`); a plan branch gated on an observation is
    escalated to `REQUIRES_REVIEW` when it is high risk and the observation is
    not fully verified, or when an environment observation is contested.
    Verification and branch resolution are recorded as `PERCEPTION_OBSERVED` and
    `BRANCH_RESOLVED` events.
  - *Periodic Revalidation* (`revalidation.py`): reuses `RecoveryEngine.assess`
    on a step interval (default 25) and on app switch, so mid-run environment
    drift is caught within one cycle instead of only at the next crash.
   - Docs: `docs/PROBLEM.md` (problem statement, honest scope) and
    `docs/RESULTS.md` (results; mini-benchmark pending). Tests:
    `tests/test_trust_gate.py`, `tests/test_revalidation_schedule.py`,
    `tests/test_toy_task_banner_attack.py`. All 740 tests pass; `ruff` and
    `mypy --strict` are clean.

- **MCP caller authentication (issue #1).** When `CONTINUUM_MCP_TOKEN` is set,
  the MCP server now refuses every mutating tool unless the caller presents that
  shared secret in the `initialize` handshake's `_meta.authToken`. The check is
  fail-closed: a missing, empty, or mismatched secret always refuses, and an
  empty configured secret refuses rather than opening the door (the closed PR
  #3 failed open on a `ValueError`). The default local, single-user, no-account
  behavior is unchanged when the variable is unset. `AuthPolicy`/`load_auth` in
  `src/continuum/mcp/authz.py`, wired into the tool `guard` in
  `src/continuum/mcp/server.py`; tests in `tests/test_mcp_authz.py`.

- **Plugin registry and capability seams (Tier 1, issue-adjacent).** New
  `continuum.plugins` package starts the "attach to any system" work from
  `references/integration-architecture.md`: a dependency-injected `Registry`
  (named services, reversible registration) and the four capability seams as
  `Protocol` interfaces, `EnvironmentProvider`, `StateExtractor`,
  `ActionReconciler`, `ValidationRule`. The first *discoverable*
  `EnvironmentProvider`, `GitProvider`, reads the current commit from a git
  repository instead of trusting a declared version, and never raises.
   Conformance tests in `tests/test_plugins.py`.

- **`continuum serve` sidecar (Tier 0 boundary, issue-adjacent).** A new,
  language-agnostic boundary so any external process or agent system can drive
  CONTINUUM's recovery operations without embedding Python or the `mcp` SDK.
  `continuum serve` speaks a tiny newline-delimited JSON protocol (request
  `{"id","method","params"}`, response `{"id","result"}` or
  `{"id","error":{"type","message"}}`) over stdio. The surface mirrors the MCP
  tool set: `record_progress`, `checkpoint`, `validate`, `resume`, `confirm`,
  `intercept_action`, `complete_action`, `fail_action`, `reconcile_action`,
  `list_actions`. Authentication is a fail-closed shared secret
  (`CONTINUUM_SERVE_TOKEN`) modeled on the MCP `AuthPolicy`. The server imports
  only the core (never `continuum.mcp`), and `serve_subprocess` launches a real
  `continuum serve` child and returns a connected client. Implementation in
  `src/continuum/serve/` (`server.py` protocol/handlers, `__init__.py` client and
  `cmd_serve` entry point wired into `src/continuum/cli/main.py`); tests in
  `tests/test_serve.py` (dispatch unit, stdio loop, and a real subprocess path).

- **CONTINUUM-Bench now proves issue #6 (idempotency under argument drift).**
  `continuum benchmark` gained a dedicated `argument_drift` scenario that drives
  the real `ActionLedger` (the same path the LangGraph/OpenAI/MCP adapters call)
  with an agent that re-attempts each external action twice using a different
  path shape (absolute vs relative). CONTINUUM dedups via a stable `key`
  (`continuum_key`) and via drift recognition (`continuum_drift`), each yielding
  0 duplicate side effects, while `naive_retry` and `replay` repeat every side
  effect (N duplicates for N actions). `IdempotencyResult`,
  `run_idempotency_benchmark`, and `render_idempotency` in
  `src/continuum/benchmark/__init__.py`; regression test in
  `tests/test_benchmark.py`. The observability half of A2 (metrics collector,
  Phase 14 dashboard, `--dashboard`) landed earlier via PR #60.

- **Real-LLM crash-and-resume harness.** `examples/langchain_real_llm_crash.py`
  drives the LangChain adapter against a live OpenRouter model through a hard crash:
  the `crash` subcommand lets the wrapped tool perform a real side effect and then
  hard-exits the process (`os._exit(137)`) before the ledger records completion; the `resume`
  subcommand runs a fresh process and asserts `RecoveryEngine.assess` blocks with
  `request_human` / `safe=False` and an outbox that still holds exactly one entry.
  `examples/openai_real_llm_crash.py` and `examples/langgraph_real_llm_crash.py`
  drive the identical contract for the OpenAI Agents SDK and LangGraph adapters. This
   proves the mid-side-effect crash contract with a live model for all three framework
   adapters. Documented in STATUS.md and `references/adapters.md`.

- **Real-LLM multi-step demo.** `examples/multitool_real_llm.py` drives the
  LangGraph adapter with one live-model prompt that orchestrates `lookup_order`,
  `notify_customer`, and `create_ticket`; each side effect is wrapped with a fixed
  idempotency key and a checkpoint is written after every tool result. It shows
  exactly-once survives the model's argument drift across a soft resume
  (`recovery: resume / safe=True`). Confirmed live that a key derived from the
  model's rendered arguments does NOT dedupe drift and must not be used. Documented
  in STATUS.md and `references/adapters.md`.

- **Framework adapters forward an explicit idempotency key.** The action ledger
  already supported a Stripe-style `key` (operation identity independent of
  argument text), but the adapters never forwarded it, so an LLM-driven tool that
  drifts its argument text between calls could not deduplicate. `GenericAgentAdapter.intercept_action`
  now forwards `key`, and all three framework adapters accept it:
  `LangChainAgentAdapter.wrap_tool`, `LangGraphAgentAdapter.wrap_tool`, and
  `OpenAIAgentAdapter.wrap_function_tool` each take `key` (a fixed string) or
  `key_fn` (derives the key from the call's `(*args, **kwargs)`); the two are
  mutually exclusive. This is the correct answer to LLM argument drift through the
  adapters, matching the `key` already accepted by `continuum_intercept_action`
  over MCP. Verified end to end against a live OpenRouter model via
  `examples/langchain_real_llm.py` (LangChain adapter); see STATUS.md for the
  recorded run. Regression tests:
  `tests/test_integration_langchain.py::TestLangChainArchitecture::test_explicit_key_deduplicates_against_argument_drift`
  and `test_key_fn_derives_key_from_call_arguments`, plus
  `tests/test_adapters_langgraph.py` and `tests/test_adapters_openai.py` key/key_fn
   forwarding tests.

- **CONTINUUM-Bench scenario suite expanded.** Added `partial_completion` and
  `early_crash` scenarios to `src/continuum/benchmark/__init__.py`, bringing the
  shipped suite to five controlled-failure scenarios. The new scenarios vary
  crash timing: `partial_completion` crashes late (most work already done) and
  `early_crash` crashes almost immediately (full replay wastes the most work).
  `tests/test_benchmark.py` asserts continuum still recovers with zero duplicate
  work and that full replay waste scales with crash timing. `model_switch` and
  the remaining spec scenarios (context compaction, tool failure, API timeout,
  file modification, permission change, stale decision) remain follow-up work
  that needs deeper harness modelling of side effects and model or decision state.

### Changed


- **README.** `Contents` laid out as a horizontal wrapping nav; Security
  Extension added to the Features table and table of contents; website link
  points to the live Vercel site; `How it works` diagram
  (`docs/assets/architecture.svg`) replaced with a complete view that includes
  the Security Extension.
- **CI.** `ruff` pinned to `0.16.2` and `ruff format` applied, so the lint
  job's format-check is reproducible (it had been failing on unpinned ruff).

### Fixed

- **The `continuum serve` sidecar's `resume` had drifted from the MCP tool it
  mirrors, so a non-Python client could not resume hands-free (issue #91).** The
  module docstring promises "the protocol mirrors the MCP tool surface so the two
  stay in sync", but two capabilities added to `continuum_resume` never reached
  the sidecar: the run `goal` in the payload (PR #80) and an optional `run_id`
  that targets the most recently active run (PR #75). A sidecar client therefore
  learned `mode` and `completed/total` but never what the task *was*, and
  `_h_resume` raised `bad_params` on the omitted `run_id` that an interrupted
  session has no way to supply. The sidecar is the boundary intended for clients
  that cannot embed Python or the `mcp` extra, so it was the one surface still
  requiring an external task file and a memorized id, the exact overhead those
  two changes removed for the MCP and CLI paths. `resume` now returns `goal` and
  accepts an absent `run_id`, reporting `mode: "no_active_run"` (matching
  `continuum_resume`) rather than a protocol error when there is nothing to
  resume. Additive: no existing key changed, and the serve-only diagnostics
  (`checkpoint_version`, `validation_reason`, `environment_changes`) are
  untouched. Trust behaviour is unchanged, since returning a self-reported goal
  confirms nothing and a self-certified run still resolves to `request_human`.
  `tests/test_serve.py` gains six regression tests, including one that diffs the
  sidecar's `resume` keys against the live `continuum_resume` payload so the next
  field added on one side and forgotten on the other fails CI instead of being
  found by hand.

- **The cannot-open-storage message escaped backslashes, so a Windows path was
  not copy-pasteable (issue #94).** Both entry points formatted the failing path
  with `!r`, and `repr()` escapes each backslash, so
  `C:\Users\ASUS\no-such-dir\agent.db` came back as
  `'C:\\Users\\ASUS\\no-such-dir\\agent.db'` — not the path the operator passed,
  and useless pasted into a shell or a config file. POSIX paths were unaffected,
  having no backslashes to escape, which is also why the MCP server's
  `test_main_reports_an_unopenable_database_instead_of_a_traceback` was red on a
  clean checkout of `main` on Windows: its `assert str(missing) in err` held only
  on POSIX. The escaping broke the exact guarantee #87 was fixed to provide.
  Both sites now use literal quote delimiters (`at '{path}'`), which still show
  leading or trailing whitespace but do not escape: `src/continuum/cli/main.py`
  and `src/continuum/mcp/server.py`. The regression test at each entry point puts
  a backslash in the *filename*, which is legal on POSIX, so the ubuntu-only CI
  can catch this class of Windows-only breakage rather than shipping it a third
  time (#81 was the first). Reported with a full diagnosis by @abyyxhek.

- **MCP server was not found at cold start because its name did not match the
  configured name (issue #87).** `.mcp.json` registered the server under the key
  `continuum`, and `build_server` advertised `MCPServer(name="continuum")`, while
  the console script, the docs, and `CLAUDE.md` all refer to it as `continuum-mcp`.
  A client that resolves the server by the `continuum-mcp` name (including the
  agent's own instructions) reported `ready: false` with `no MCP server with this
  name is configured: continuum-mcp`, so the first tool call failed until a manual
  `/mcp` reconnect. Both the `.mcp.json` key and the advertised server name are now
  `continuum-mcp`, so the server is discovered and connected on the first attempt
  with no per-session reconnect. The separate leak and clean-diagnostic hardening
  of the cold-start path is tracked under #87 as well.

- **A failed MCP cold start leaked a database handle and reported itself as a
  traceback (issue #87).** `build_server` opened storage on its first line but
  resolved the authorization policy and auth token after it, and both loaders
  reject malformed input with `ValueError`. A bad policy file or a
  `CONTINUUM_MCP_CLIENT_TOKENS` entry without a colon therefore stranded an open
  `SQLiteStorage` with no owner to close it, the same leak as issue #81 and fatal
  on Windows for the same reason, and left an empty database behind for a server
  that never started. Configuration is now resolved before storage is opened, so
  nothing is acquired until it can be used. `main` also called `build_server`
  outside any handler, so an ordinary operator mistake surfaced as a
  `sqlite3.OperationalError` or `ValueError` traceback; over stdio that goes into
  the protocol pipe, where the client reports only that the server never became
  ready. It now prints the CLI's `error: ...` form to stderr and exits 1,
  matching the rationale already documented in `cli/main.py`. Tests in
  `tests/test_mcp_server.py`.

- **A `continuum-mcp` installed without its optional SDK died with a
  `ModuleNotFoundError` traceback (issue #87).** The `mcp` extra is optional, but
  `[project.scripts]` installs the `continuum-mcp` console script
  unconditionally, so a plain `pip install continuum` produces an entry point
  whose dependency is absent — and `mcp/server.py` imported `MCPServer`,
  `Context` and `ToolAnnotations` at module scope. The process therefore died
  during import, before the `initialize` handshake and before any handler in
  `main` could run, so the client reported only that the server never became
  ready while the traceback went to a stderr log nobody was reading. This is the
  same class of failure as the `ValueError`/`sqlite3.Error` cold starts above,
  but it was out of reach of those handlers because it happened at import time.
  The three SDK imports now live inside `build_server` (with a `TYPE_CHECKING`
  import for the return annotation), and `main` prints
  `error: the MCP server needs the optional 'mcp' dependency ... pip install
  'continuum[mcp]'` to stderr and exits 1. The handler is narrowed to the SDK
  itself, so a missing transitive dependency of some other package keeps its
  traceback instead of being misreported as a missing extra. Importing
  `continuum.mcp` no longer requires the extra either. Tests in
  `tests/test_mcp_server.py`.

- **`continuum benchmark` crashed on Windows from unclosed database handles
  (issue #81).** `_run_one` and `run_idempotency_benchmark` constructed
  `SQLiteStorage` without ever closing it, so the enclosing
  `TemporaryDirectory()` still held open `.db` files at cleanup. POSIX allows
  unlinking an open file, so this was an invisible resource leak on Linux and
  macOS; Windows refuses it, and the whole command died on an unhandled
  `PermissionError`. Both call sites now use `with SQLiteStorage(...) as store:`,
  matching every other call site in the codebase. `tests/test_cli.py::_cli` also
  replaced the subprocess environment with a hardcoded POSIX `PATH`, dropping
  `SystemRoot` and leaving spawned interpreters unable to initialise Winsock on
  Windows; it now inherits the parent environment and overrides only
  `PYTHONPATH`. Together these fixed five tests that failed on Windows.

- **Three defects found by an adversarial audit of the MCP surface**, driven over
  the live stdio protocol with every tool result verified against the SQLite store
  rather than taken at its word. Method and per-claim results in `test.md`:
  - *Environment drift was detected but invalidated nothing.* `continuum_checkpoint`
    passed `env` to `capture_state` as an `EnvironmentSnapshot` only, and
    `StateValidator._apply_dependency_status` returns early for a state with no
    `external_dependencies` — so a moved dataset was rendered in
    `environment_changes` while the verdict stayed `safe: true` with the reason
    "all components verified against the current environment". The core validator
    was never wrong: given a declared dependency it already yields `CONFLICTED`
    and `safe_to_resume=False`. The gap was that no MCP client could declare one,
    and the existing test appended `DEPENDENCY_DECLARED` straight to storage.
    Checkpointing now records each pinned resource as a `DEPENDENCY_DECLARED`
    event, so the declaration is durable across projections and restores, covered
    by the hash chain, and carries `EXTERNAL_AGENT` provenance — which does not
    weaken the check, since a dependency's status comes from comparing two
    snapshots rather than from trusting the claim. Only new or re-pinned resources
    are appended, so checkpointing on a schedule does not grow the log. The
    `serve` sidecar shared the defect verbatim and is fixed identically; the two
    surfaces must not disagree about whether drift is safe.
  - *`continuum_list_actions` under-reported an interrupted action.* A claim left
    `STARTED` by a crash reported `side_effect_uncertain: false` while
    `continuum_resume` described the same action as an unknown outcome — the
    aggregate `unresolved` count was right while the row a human reads said the
    opposite. `side_effect_uncertain` is only set on escalation to `UNKNOWN`,
    which has not happened yet for a fresh interruption. Each row now carries
    `outcome_unresolved`, derived from ledger state so it cannot drift from what
    recovery reports. Also fixed in the `serve` sidecar.
  - *WAL "self-healing" could destroy committed transactions.*
    `_open_server_storage` deleted both sidecars on a startup `OperationalError`,
    on the stated grounds that they are reconstructable from the main database.
    That holds for `-shm` and not for `-wal`, which carries transactions committed
    but not yet checkpointed; measured on a real database the main file was 4 KB
    while the WAL held all 16 events, and deleting it lost everything *silently*,
    because an emptied database still verifies as an intact chain. Recovery is now
    staged least-destructive-first: discard the reconstructable `-shm` and retry,
    and only if that fails move the `-wal` aside — never unlink it — restoring it
    if the retry fails anyway, and warning on stderr with the quarantine path when
    it succeeds. Reachable only when the initial open raises, so latent rather
    than observed, but it is exactly the hard-kill path the feature advertises.

- **Six correctness defects found by triaging the open bug backlog (issues #29,
  #30, #33, #36, #42, #43, #45).** Each is covered by a test that fails on the
  previous code:
  - *#33 / #36: the ledger's argument-drift fallback ignored whole classes of
    resource identity.* `_is_strong_token` required a digit, `@`, or `.`, so a
    plain-word identity (`invoice`, `dataset`) was discarded, and purely numeric
    ids were discarded outright; separately, `identity_tokens` only tokenised
    `str`, so an integer id such as `4821` never became a token at all. Both are
    real identities now. Admitting plain words would let one shared adjective
    ("both tickets are `urgent`") collapse two distinct actions into one, silently
    dropping the second side effect, so `ActionLedger._identity_match` no longer
    matches on a single shared token: it requires one token set to *contain* the
    other, compared at the leaf (`leaf_tokens`), so a path counts as its basename
    and an absolute-vs-relative re-rendering still deduplicates while genuinely
    different resources do not.
  - *#45: `claim(on_unknown=...)` did not persist its resolution.* A resolver's
    `ActionOutcome` was returned to the caller but nothing was recorded, so the
    stored action stayed `UNKNOWN`: the next claim re-raised, `pending()` never
    drained, and `RecoveryEngine.assess` asked for a human forever. The
    resolution is now written as an `ACTION_RECONCILED` event.
  - *#29: `reconcile(occurred=False)` left stale evidence behind.* An action
    just decided never to have happened kept the `external_id` and `result` from
    its earlier `COMPLETED` state. Both are cleared.
  - *#42: `strict_unknown` was silently ignored for uncertain side effects.* The
    engine escalates an unknown side effect to `REQUEST_HUMAN`, but `plan_repairs`
    emitted a `reconcile_action` step with `requires_human=False`, so
    `plan.requires_human` was `False` and the contract permitted the agent to
    auto-reconcile against the mode. The step now requires a person in strict
    mode, and its `reason` reports what happened (interrupted) rather than
    mislabelling it "escalated for review".
  - *#43: `continuum history` hid checkpoints.* `put_version` returns the same
    version when the state fingerprint is unchanged, so keying the listing by
    version collapsed two checkpoints into one row. It now lists checkpoints;
    the JSON key is `checkpoints` rather than `versions`.
  - *#30: a deleted tracked file diffed as "changed" instead of "removed".*
    `FileProvider` recorded a missing file as a resource with `version=None`;
    it now omits it, which `diff_environments` reads as `REMOVED`.

- **`StateValidator._check_model` reported model-specific assumptions
  `VALID` when the resume model was unknown (fail-open).** When
  `expected_model` is `None` (e.g. `continuum validate`/`resume` run without
  `--model`) or the state itself doesn't record which model produced it,
  the validator has no way to verify recorded model-specific assumptions —
  but it reported them `VALID` and left `safe_to_resume=True` anyway,
  contradicting the module's own rule that it may say "I cannot tell" but
  must never guess in its own favour. `_check_model` now reports `UNKNOWN`
  in both cases, which `_UNUSABLE` correctly turns into a blocked resume
  under the default `strict_unknown=True`. Reported as issue #49 and
  covered by
  `tests/test_validator.py::test_no_expected_model_with_assumptions_is_unknown_not_valid`
  and `::test_unrecorded_model_with_assumptions_is_unknown_not_valid`.

- **`StateValidator._check_progress` no longer downgrades self-certified
  progress to `UNKNOWN`.** The "no source events" check (`source_sequence == 0
  and completed > 0`) ran as a second `if` after the self-certified branch, so a
  self-reported progress (the shape the OpenAI and LangGraph adapters emit) was
  relabelled `UNKNOWN` and then silently unblocked by `--tolerate-unknown`
  (`strict_unknown=False`). `UNKNOWN` is excepted under `strict_unknown=False`,
  but `REQUIRES_REVIEW` is not, so a self-report must always block a resume. The
  second check is now an `elif`, so it cannot overwrite a `REQUIRES_REVIEW`.
  Fixed in issue #48.
- **OpenAI Agents SDK adapter could not run a real tool call.** Two bugs in
  `OpenAIAgentAdapter.wrap_function_tool` surfaced only when an actual model drove
  the agent (verified against a live OpenRouter model; see STATUS.md). First, the
  generated wrapper typed every parameter as `Any`, so the SDK emitted a tool JSON
  schema with no `type` key, which OpenRouter rejects (`invalid_function_parameters`).
  The wrapper now preserves the original parameter annotations via
  `inspect.formatannotation`. Second, the adapter overrode `__signature__` to drop
  the `ctx` parameter, so `function_schema` never saw a `RunContextWrapper` first
  argument and concluded the tool took no context, feeding the raw tool-input
  string as the first positional instead. The context parameter now stays first in
  the inspectable signature, annotated `RunContextWrapper`, so the SDK passes the
  run context and the adapter can extract the run id and intercept the side effect.
  Regression test: `tests/test_adapters_openai.py::TestWithRealOpenAIAgents::test_wrap_function_tool_keeps_param_types_in_schema`.

- **`continuum_intercept_action` deduplicated on argument formatting, not
  resource identity.** The idempotency key hashes the action type plus the
  caller's raw arguments, so two sessions describing the same operation with
  different argument shapes (relative vs absolute path) computed different keys
  and the resumed session was told `proceed: true` for a side effect the first
  session already completed. Found by the issue #6 end-to-end series: three real
  Claude Code runs all hit it, and correctness survived only because the agents
  cross-checked the outbox and refused the flag. The tool now accepts a stable
  `key` (e.g. `invoice:INV-001`) passed through to `ActionLedger.claim(key=...)`;
  two attempts sharing action type and key are the same action regardless of
  argument formatting, so dedup is immune to path/argument drift. The tool
  description tells callers to derive the key from the resource identity. A
  regression test mirrors the e2e failure: intercept and complete with
  `key="invoice:INV-001"` and relative-path arguments, then intercept again with
  the same key and absolute-path arguments, and assert `proceed: false`.

- **Dedup still failed when the caller supplied no stable key (transcript
  analysis).** Re-reading the three e2e transcripts showed the real drift was
  argument *field names* (`target` vs `outbox_file` vs `outfile` vs `file`) and,
  in one run, the action type itself (`send_invoice` vs `send-invoice-email`),
  with `external_id` shape drift (absolute path vs bare basename). Path
  canonicalization alone cannot bridge field renames, and no stable key helps
  when the agent forgets to pass one. Two defensive layers now cover this:
  `arguments_hash`/`idempotency_key` canonically normalize path-like arguments
  (lexical `normpath` plus `~` expansion, URLs untouched) so equivalent path
  spellings hash identically; and `ActionLedger.claim()` gains a token-based
  identity fallback for the no-explicit-key case, recognizing an already
  recorded action of the same type by shared identity tokens (scalar values,
  path basenames and stems, external ids; weak tokens such as counts and status
  words are dropped). A unique completed match returns `fresh=False` with the
  stored result; a unique interrupted match surfaces as uncertain rather than
  opening a fresh slot; ambiguity and the run id plumbing token never match.
  Regression tests mirror each observed drift shape.

- **MCP server fails to connect after a hard-kill (orphaned WAL sidecars).** A
  server process killed with `SIGKILL` cannot run SQLite's WAL cleanup, so it
  leaves `<db>-wal` and `<db>-shm` sidecars behind. On the next launch, opening
  the database in WAL mode could raise `sqlite3.OperationalError: disk I/O error`
  at `PRAGMA journal_mode=WAL`, crashing the server before it served a single
  request and surfacing to the client as `Failed to connect`. `ContinuumMCP` now
  opens its store through a new `_open_server_storage(database)` helper that, on
  that error, removes the orphaned sidecars and retries the open exactly once;
  when there is nothing to remove it re-raises, so an unrelated disk error still
  surfaces. The recovery is confined to the MCP server startup path: the
  library's `journal_mode=WAL`, `synchronous=FULL`, and IMMEDIATE-transaction
  guarantees in `storage/sqlite.py` are unchanged. Two regression tests in
  `tests/test_mcp_server.py` cover the recovery and the re-raise.

- **`examples/` fail `ruff check`.** The three example scripts carried 13 lint
  violations (E402, F401, F541, E841) that CI never saw because the lint and
  format steps only checked `src/ tests/`. The violations are fixed, the scripts
  are reformatted, and the CI `ruff check`/`ruff format --check` steps now
  include `examples/`. Fixes #8.

- **OpenAI adapter cannot auto-provision a fresh run (issue #21).**
  `OpenAIAgentAdapter._ensure_run_exists` assumed `Storage.get_run` returns
  `None` for a missing run and guarded its `create_run` call with
  `if existing is not None`. `get_run` actually raises `RunNotFound` (it never
  returns `None`), so the create branch was unreachable and the first contact
  with a new run failed with `RunNotFound` instead of provisioning it. The
  method now catches `RunNotFound` and creates the run, so a fresh OpenAI agent
  run is auto-provisioned on `on_agent_start`. Two regression tests in
  `tests/test_adapters_openai.py` cover the create-on-missing path and the
  idempotent existing-run path.

- **Wrong clone URL in CONTRIBUTING.md.** `git clone
  https://github.com/continuum-agent/continuum.git` pointed at a repository
  that does not exist; the correct URL is `git clone
  https://github.com/Cyrax321/CONTINUUM.git`.

- **Stale Roadmap table.** Rows 9 (crash recovery examples) and 11
  (framework adapters) read "Planned" despite the examples existing and
  running to completion and the generic/LangGraph/OpenAI adapters being
  built. Both are now marked Complete, and the "Planned framework adapters"
  note below the table now reads "Built".

- **`continuum events` now honours the not-found exit code (issue #18).** It
  previously printed "No events." and exited 0 for a run that was never created,
  unlike every other run-scoped command which exits 2. `cmd_events` now gates on
  `get_run` (which raises `RunNotFound`, mapped to `NOT_FOUND` by the dispatcher),
  and `events` is added to the missing-run parametrised test so the contract is
  enforced.

- **CI Node 24 migration.** Bumped all GitHub Actions workflow pins to versions
  that run on Node 24, eliminating deprecation warnings and pre-empting the
  hard failure when GitHub ends its Node 20 grace period. `actions/checkout`
  v4→v7.0.1, `actions/setup-python` v5→v7.0.0, `codecov/codecov-action`
  v4→v7.0.0, `actions/upload-artifact` v4→v7.0.1, `actions/download-artifact`
  v4→v8.0.1, `actions/configure-pages` v5→v6.0.0, `actions/deploy-pages`
  v4→v5.0.0, `softprops/action-gh-release` v2→v3.0.2. Applied across `ci.yml`,
  `release.yml`, and `deploy-pages.yml`. `actions/upload-pages-artifact@v3` left at
  v3 and `pypa/gh-action-pypi-publish@release/v1` left unchanged: both run as
  composite actions, not on Node, so they are not affected.

- **Older-schema databases opened silently, then failed with a raw sqlite
  error (issue #17).** A pre-v2 file opened without `SchemaVersionError`
  (only *newer* versions were rejected), `read_events` returned `[]` for a
  populated run, and the first write failed with
  `OperationalError: table events has no column named event_id`, which did
  not name the real cause. `_migrate` in `src/continuum/storage/sqlite.py`
  now raises `SchemaVersionError` when the stored schema version is below
  `SCHEMA_VERSION`, mirroring the existing newer-version guard. There is no
  automatic migration path, so the error tells the operator to reset the
  database or open it with a compatible build. Reproduced from the report's
  v1 fixture; the fix is covered by
  `tests/test_storage.py::test_an_older_schema_is_refused`, which writes a
  v1 `continuum_meta` row and asserts the open is refused with "older
  CONTINUUM". Closed by commit `82b9f1c`.

- **`continuum resume --repair` recorded nothing; the flag was a no-op (issue
  #19).** The help text and `cmd_resume` docstring said `--repair` records the
  repair plan, but it only suppressed the stderr hint and left the database
  unchanged. `cmd_resume` in `src/continuum/cli/main.py` now appends a
  `RECOVERY_STARTED` event carrying the assessment's plan steps (kind, target,
  reason, requires_human) whenever `--repair` is given and a plan exists, and
  confirms the write on stderr. Omitting `--repair` remains strictly read-only.
  Covered by `tests/test_cli.py::test_repair_records_the_plan_and_does_not_fake_a_safe_exit`
  (asserts the `RECOVERY_STARTED` event is written with its mode and plan) and
   `tests/test_cli.py::test_resume_without_repair_is_still_read_only`. Closed by
   commit `f145818`.

 - **`continuum replay` claimed to verify the stored version but never compared
   it (issue #31).** `cmd_replay` re-derived state from events and reported
   success without checking the recomputed version against what is persisted, so
   a corrupted or drifted store passed silently. It now actually verifies the
   stored version and fails when they diverge. Closed by `a5c3307` (PR #50).

 - **`continuum replay --upto N` crashed with `ProjectionError` when the prefix
   excluded `RUN_STARTED` (issue #32).** The projector needs the run's start
   event to seed state, so a prefix that drops it raised instead of reporting a
   clear error. `cmd_replay` now rejects `--upto` values that exclude
   `RUN_STARTED` with a message explaining the constraint. Closed by `fd1bf90`.

 - **`continuum_record_progress` accepted negative `completed`/`failed` when
   `total` was omitted (issue #38).** A missing total let callers poison the log
   with negative counters that no downstream check caught. The progress writer
   now rejects negative `completed`/`failed` even when `total` is absent. Closed
   by `fca1b6e` (PR #51).

 - **`LLMExtractor.extract()` crashed on a malformed LLM proposal (issue #40).**
   A proposal that failed to parse raised out of `extract()` instead of degrading
   to the deterministic state. It now falls back to the deterministic state on a
   malformed proposal rather than propagating the exception. Closed by `8c7cfec`
   (PR #56).

 - **`LLMExtractor._merge` double-added ids repeated within a single proposal
   (issue #41).** Ids that appeared more than once in one LLM proposal were
   merged additively, inflating the projected state. `_merge` now collapses ids
   repeated within a single proposal. Closed by `a1bdef4` (PR #52).

 - **`intercept_action` returned a divergent value on a cache hit when the result
   dict held the reserved key `__return_value__` (issue #44).** A cached result
   whose payload used the envelope key was reshaped differently from a fresh one,
   so callers could see two shapes for the same action. The adapter now keeps a
   result dict holding the envelope key intact on cache hit. Closed by `15e0d67`
   (PR #53).


### Added

- **Regression test for the checkpoint environment round-trip.** `tests/test_storage.py`
  exercises the checkpoint/reload path end to end: a checkpoint is written with a
  declared dependency and a captured `EnvironmentSnapshot`, the `SQLiteStorage` handle is
  closed, a *fresh* `SQLiteStorage` is opened on the same file, and the environment is
  asserted to survive the round-trip. The reloaded run is then assessed against an
  unchanged environment and must resume as safe — proving `StateValidator.validate_dependency`
  sees the dependency as unchanged rather than treating a missing baseline as
  *added/breaking*. This path (serialising `StateCheckpoint.environment` through the
  checkpoint `body` column and restoring it) had no coverage; this is added test
  coverage for an untested path, not a fix for a defect.

### Added — Phase 12: CONTINUUM-Bench (minimal harness)

- **`continuum benchmark` now runs a real benchmark instead of exiting 4.** The
  harness (`src/continuum/benchmark/__init__.py`) measures three scenarios that
  break naive recovery (process crash, dataset change while the agent is down,
  interrupted external side effect) across three strategies: `continuum`
  (semantic checkpoint plus environment revalidation plus action ledger),
  `replay` (full transcript replay from scratch), and `naive_checkpoint`
  (resume from the saved progress count, no validation).
- **Numbers are measured, not invented.** Each run drives the actual library
  (`SQLiteStorage`, `CheckpointManager`, `ActionLedger`, `StateValidator`,
  `build_recovery_context`) against an in-process simulated agent; nothing is
  mocked. Reported per run: duplicate work ratio, duplicated external side
  effects, whether the strategy detected the stale environment, and the size of
  the recovery briefing versus the full event log (compression ratio).
- `benchmark` takes `--total N` (documents per run, default 200) and `--json`
  for machine consumption. `tests/test_benchmark.py` asserts the continuum
  strategy shows zero duplicate work, exactly one side effect, detects the
  dataset change while the naive strategy does not, and that replay wastes work.

### Added — Phase 8: command-line interface

- `continuum` CLI covering `init`, `runs`, `inspect`, `history`, `events`, `diff`, `validate`,
  `resume`, `checkpoint`, `verify`, `actions`, `show-contract`, `replay` and `benchmark`.
- **Built on `argparse` from the standard library, not `click`.** The moment you most need to
  inspect a broken run is the worst possible moment to discover your diagnostic tool cannot import
  its dependencies. The `cli` extra was removed from `pyproject.toml`; no extra is required.
- **Exit codes carry the verdict.** `continuum resume "$RUN" && ./start-agent.sh` must never launch
  an agent onto stale state, so only a verified-safe run exits 0. Distinct codes (`10` repair,
  `20` human, `30` unsafe, `2` not found, `3` corrupted, `4` not implemented) let automation react
  proportionately without parsing text. An unclassified mode falls through to `UNSAFE`, never `OK`.
- Read-only commands are genuinely read-only, asserted by a parametrised test that snapshots event
  count and version list around all nine of them.
- `--json` on every command for machine consumption; text and JSON are never mixed on one stream.
- `--env NAME=VERSION` declares the current environment. Omitting it yields `None`, which the
  validator treats as *unverified* rather than *unchanged* — not checking must not resemble
  checking and finding nothing wrong.
- `benchmark` exits `4` and states plainly that no numbers are published because none have been
  measured.
- 48 new tests (473 total), including real-subprocess invocation and a shell-pipeline test proving
  `&&` short-circuits on unsafe state.

### Fixed

- **`verify` and `actions` exited 0 for a run that does not exist.** An absent run has a trivially
  valid (empty) event chain and no recorded actions, so `continuum verify $TYPO && deploy` reported
  a clean bill of health for a name nobody had ever written to — precisely the failure the
  exit-code contract exists to prevent. All eight run-scoped commands now check existence first and
  report `NOT_FOUND` consistently. Found by driving the installed binary by hand; the test suite
  had not covered it.
- `replay` on a missing run reported "the log never recorded RUN_STARTED", diagnosing the wrong
  problem.
- `RunNotFound` and `CheckpointNotFound` inherit from `KeyError`, whose `__str__` applies `repr()`
  to the message, so users saw `error: "no such run: 'ghost'"` — quoted twice.
- CLI output was written to a block-buffered stdout while hints went to stderr, so when piped the
  hint could appear *before* the report it referred to. Output is now flushed at each emit.
- `render_diff` duplicated the field name for progress counters (`completed: completed: 1 → 50`).
- A weak test: `test_every_recovery_mode_maps_to_a_code` iterated only *known* modes, so it never
  reached the unmapped-mode fallback it claimed to protect. Mutation testing caught that defaulting
  the fallback to `OK` went undetected; the replacement exercises an unclassified mode directly.

### Added — Phase 7: recovery engine

- `RecoveryEngine` (`continuum.recovery.engine`) reducing three independent signals — validation
  statuses, action-ledger state and checkpoint integrity — to one `RecoveryMode`.
- **The most cautious applicable signal wins.** Each signal proposes a mode and the engine takes the
  maximum on an explicit severity ordering (`RESUME < REPAIR_AND_RESUME < REPLAN < WAIT <
  REQUEST_HUMAN < ROLLBACK < ABORT`). These signals genuinely co-occur — a run can have a stale
  dataset *and* an uncertain side effect — and returning whichever was noticed first would let the
  unsafe answer win roughly half the time.
- `plan_repairs` (`continuum.recovery.planner`) producing an ordered, deduplicated, deterministic
  repair plan. Reconciling an uncertain side effect always sorts first: nothing else is safe while
  the world may or may not have been modified. Dependencies are re-pinned before the evidence and
  findings derived from them, since repairing in the wrong order produces work that is stale on
  arrival.
- Components with no mapped repair escalate to human review rather than passing silently, so an
  unhandled case cannot be mistaken for a clean one.
- `RecoveryContract` (`continuum.recovery.contract`) naming exactly **one** next permitted action.
  Listing everything currently allowed would let an agent pick the convenient step and skip the
  reconciliation it was supposed to do first. Contracts are deterministic and sealed with an
  integrity hash — a contract editable between issue and enforcement would gate nothing.
- The engine is read-only: it computes and explains a decision without mutating the run, which is
  what makes assessment safe to perform against a live database.
- 49 new tests (424 total), including a precedence matrix and five mutation checks confirming the
  decision logic resists sabotage. 100% line coverage.

### Fixed

- `strict_unknown=False` was honoured by the validator but ignored by the planner, so unverifiable
  resources still demanded human review and the setting had no observable effect.
- Removed a dead `now` parameter from `StateValidator` that was accepted, typed as `object`, and
  never used.

### Removed

- Two unreachable branches in the engine's decision rule (`ABORT` on an empty run, and an
  empty-proposal fallback). `restore()` raises before the first can be reached and the second cannot
  fire; both were verified dead rather than left as untestable code.

### Added — Phase 6: action ledger and idempotency

- Idempotency keys (`continuum.actions.idempotency`) derived from action type plus canonically
  hashed arguments, so argument order never matters but a changed value always does. `scope`
  separates runs; `volatile` excludes fields like retry counters that would otherwise make every
  retry look like a new action. Nothing is excluded by default — collapsing two genuinely different
  operations into one would silently skip real work.
- `ActionLedger` (`continuum.actions.ledger`) implementing claim -> perform -> complete, stored as
  events so it inherits the log's ordering, durability and tamper-evidence. A repeat claim for a
  completed action returns the stored result and external id instead of performing it again.
- **`UnknownSideEffect` instead of a guess.** When a crash lands between the effect and its record,
  the ledger cannot tell whether it happened, and neither retrying nor skipping is safe by default.
  It raises and requires reconciliation. Every crash interleaving is enumerated in the module
  docstring.
- A timeout is treated as uncertainty, not absence: `fail(..., certain=False)` records `UNKNOWN`
  rather than `FAILED`, because a request that timed out may still have been processed.
- Reconciliation strategies (`continuum.actions.reconciliation`): `ProbeReconciler` (ask the
  external system — the only strategy producing evidence), `AssumeNotOccurredReconciler` (requires
  explicitly asserting `idempotent=True`), and `ManualReconciler` (escalates). A probe that raises
  is treated as "could not determine", never as evidence of absence. There is deliberately no
  `AssumeOccurred` strategy: assuming success without evidence silently drops work, and a dropped
  side effect is invisible.
- Per-action-type reconciler mapping, so a file upload can be retried while a payment escalates.
- 46 new tests (375 total), including three real-subprocess tests that crash after performing a
  side effect and assert the external system ends with exactly one record. 100% line coverage.

### Fixed

- `SQLiteStorage` now closes its connection on finalisation, so a dropped handle does not leak a
  file descriptor. Documented as a safety net, not a substitute for `close()`.

### Added — Phase 5: state validation

- Environment capture (`continuum.environment.snapshot`): pluggable `EnvironmentProvider` with
  `StaticProvider`, `FileProvider` (content hashes, so touching a file does not invalidate work),
  `ValueProvider` and `CallableProvider`. Providers never raise — a resource that cannot be
  inspected is recorded as `UNKNOWN_VERSION`, because an environment check that fails open defeats
  the purpose of checking.
- Environment diffing (`continuum.environment.diff`) distinguishing `UNCHANGED`, `CHANGED`, `ADDED`,
  `REMOVED` and `UNKNOWN`. `UNKNOWN` is not a softer `UNCHANGED`: an unverifiable resource is
  treated as breaking, so uncertainty degrades rather than resolves in the system's favour. Adding a
  resource is explicitly non-breaking; checksums outrank version labels as identity.
- `StateValidator` (`continuum.state.validator`) checking every component against the environment as
  it is *now*, and returning a `ValidationOutcome` whose `state` already carries the revised
  statuses so callers need not re-derive them.
- **Staleness propagation** along `dependency -> evidence -> finding -> decision`. A dataset moving
  v3 to v4 does not only invalidate the dependency; it invalidates the reasoning built on it.
  Marking only the dependency would leave an agent reasoning from conclusions it can no longer
  justify. State that did not depend on the change is left untouched.
- Approval expiry (by status and by timestamp), model-switch detection that never assumes switching
  is safe, and detection of state citing support it cannot produce.
- `strict_unknown` (default on) decides whether unverifiable resources block a clean resume.
- 52 new tests (329 total), 100% line coverage maintained.

### Fixed

- `SemanticState.dangling_evidence()` reported a false alarm for any decision citing a *finding*
  rather than raw evidence — which is legitimate provenance and occurs in every well-formed
  reasoning chain. Findings now count as citable support. False alarms are how real ones get
  ignored.

### Removed

- A dead branch in progress validation that re-checked a counter invariant the `Progress` model
  already enforces on construction and deserialization. Verified unreachable rather than left as
  untestable code; the invariant is tested at the model level.

### Added — Phase 4: checkpoint creation

- Checkpoint policies (`continuum.checkpoint.policy`): `ManualPolicy`, `IntervalPolicy`,
  `EventPolicy`, `SemanticPolicy`, `ContextPressurePolicy` and `HybridPolicy`, plus a
  `default_policy()` that checks explicit requests, side effects and meaning before falling back to
  time — so a checkpoint reports the real reason it was taken rather than "the timer went off".
  Policies are pure functions of an explicit `PolicyContext`, including the clock, which makes
  checkpoint timing testable instead of flaky.
- `SemanticPolicy` fires on meaning, not volume: structural changes (a decision recorded or
  invalidated, a dependency version change, an approval, a model switch) always checkpoint, while
  incremental progress only checkpoints on crossing a configurable stride.
- `CheckpointManager` (`continuum.checkpoint.manager`): evaluates policy, projects state, writes
  version then checkpoint then annotation, and restores. The write ordering is documented against
  each crash interleaving; no ordering can produce a checkpoint that claims to cover events it does
  not.
- `restore()` replays events recorded after the checkpoint onto it, so a crash *between* checkpoints
  does not discard the work in between. `replay=False` returns the checkpoint on its own terms for
  validators that must judge it before trusting anything newer.
- Bounded recovery context (`continuum.checkpoint.context`): renders the minimum sufficient briefing
  — goal, verified progress, stale state, items requiring review, valid decisions, pending work,
  findings ranked by confidence, dependencies. Sections drop from the least important end under a
  token budget, but goal, progress and stale state are never dropped: an agent that resumes without
  knowing what to distrust is worse than one that does not resume.
- Token counts are explicitly labelled heuristic estimates (character-based). CONTINUUM takes no
  tokenizer dependency, and no compression ratio is claimed until the benchmark measures one.
- 71 new tests (277 total), 100% line coverage maintained.

### Fixed

- A checkpoint's own `STATE_CHECKPOINTED` annotation was counted as unreplayed work, so every
  freshly-checkpointed run looked stale and restore replayed a no-op event each time. The manager
  now advances the cursor past its own annotation, with a fallback for the crash interleaving where
  the annotation was never written.

### Added — Phase 3: SQLite persistence

- `Storage` interface (`continuum.storage.base`) covering runs, events, state versions and
  checkpoints, with its guarantees and non-guarantees documented in the module itself: append-only
  events, atomic sequence allocation and durability on commit are promised; exactly-once,
  distribution and encryption at rest explicitly are not.
- `SQLiteStorage` (`continuum.storage.sqlite`): WAL journaling so readers never block the writer,
  `synchronous=FULL` so committed work survives power loss, enforced foreign keys, `IMMEDIATE`
  write transactions, and a `UNIQUE(run_id, sequence)` backstop that turns a write race into a
  `ConcurrentWriteError` instead of a silently forked chain.
- Optimistic concurrency via `expected_sequence`, letting a caller detect that a run moved on
  beneath it rather than blindly appending.
- `verify_events` re-audits a persisted chain directly from SQL, reporting `trusted_through` and
  flagging unreadable rows without raising.
- Integrity on read: corrupted runs, versions and checkpoints raise `CorruptedRecord` rather than
  returning untrustworthy state. Checkpoints are sealed with an integrity hash on write.
- `Run` model and sealed `StateCheckpoint` (`content`/`digest`/`sealed`/`verify`).
- `open_storage()` URL handling for `sqlite:///path`, bare paths and `:memory:`; PostgreSQL fails
  with a clear `NotImplementedError` instead of silently falling back to a local file.
- 65 new tests (206 total), including two OS processes racing on one database file and a hard
  `os._exit` mid-run, verified to resume with zero duplicated work.

### Fixed

- Event payloads are now validated as JSON-native at construction. A `datetime` in a payload hashed
  one way in memory and another way after being read back, which would have made a valid event fail
  reload — phantom corruption caused by storage, not by tampering.
- `sqlite://` URL parsing no longer strips the leading slash of an absolute path, which had caused
  the database to be created in the working directory instead of the requested location.

### Added — Phase 2: semantic state representation

- Deterministic projection (`continuum.state.semantic`): folds an event prefix into `SemanticState`.
  Guarantees reproducibility (no wall-clock dependence) and prefix-closure, so a run can be recovered
  up to the log's `trusted_through` boundary. Unknown event types are counted and reported rather
  than raising, keeping forward-written logs recoverable.
- `Provenance` and `Origin` on every state component: each item traces back to the event that
  produced it, and `reproducible` distinguishes re-derivable state from asserted or inferred state.
- Pluggable extraction (`continuum.state.extractor`): `StateExtractor` protocol,
  `DeterministicExtractor` (no model, no network), optional `LLMExtractor` that may only add
  components — never modify or delete recorded facts — tagging everything `Origin.LLM` and
  `REQUIRES_REVIEW`, and degrading to the deterministic result if the callable raises.
  `CompositeExtractor` chains extractors without double-applying events.
- Content-addressed version chain (`continuum.state.versioning`): linked, verifiable history that
  refuses to record semantically unchanged states.
- Semantic diff (`continuum.state.diff`): ID-based comparison that ignores reordering, separates
  `INVALIDATED` from `CHANGED`, produces deterministic output, and renders for the CLI.
- 11 new event types: findings, work, dependencies, approvals and model identity.
- `SemanticState` accessors used by validation and recovery, including `dangling_evidence()` for
  detecting state that cites support it cannot produce.
- 84 additional tests (141 total), 100% line coverage of `src/continuum`.

### Added — Phase 1: data models and event system

- Durable data model (`continuum.models`): semantic state tree (goal, plan, progress, decisions,
  findings, evidence, pending work, approvals, external dependencies, model state), action ledger
  records, environment snapshots, validation reports, recovery contracts, checkpoints and diffs.
- Status vocabularies as `StrEnum`: `StateStatus`, `ActionStatus`, `RunStatus`, `ApprovalStatus`,
  `RecoveryMode`, `RecoverySafety`, `Component`, `DiffKind`, `PlanStepStatus`.
- Append-only, hash-chained event log (`continuum.events`) with per-run sequencing, sealed events,
  chain reload validation and an integrity audit reporting `trusted_through` per run.
- Deterministic canonical hashing (`continuum.security.hashing`) with sorted keys, UTC-normalised
  timestamps, enum-by-value serialization, and explicit rejection of non-finite floats, sets and
  ambiguous mapping keys.
- Test suite: 57 tests covering model invariants, immutability, serialization determinism, chain
  linkage, tamper/deletion/fork detection and property-based version monotonicity.

### Notes

- No runtime, storage engine, validator, ledger logic, recovery engine or CLI yet.
- No benchmark results are claimed; the harness does not exist.

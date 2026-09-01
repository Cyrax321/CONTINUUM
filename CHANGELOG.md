# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`hooks install` wires the compaction checkpoint (#449).** Claude Code fires
  `PreCompact` immediately before it compacts the transcript: the one
  interruption the harness announces in advance, and until now the only
  lifecycle hook the installer left for the operator to hand-edit into
  `.claude/settings.json`. Forgetting it meant compaction could discard
  reasoning that was never recorded, which is the loss this project exists to
  prevent. `continuum hooks install claude-code` now writes a `PreCompact`
  entry running the new `continuum precompact` command, alongside the
  SessionStart and PostToolUse hooks; `--no-precompact` skips it and
  `hooks remove` takes it out with the rest. It is on by default, unlike
  `--with-gate`, because a gate can deny a tool call and so changes how the
  agent behaves, while this only seals state the run already has.

  The documented recipe could not be installed verbatim: it names one run
  (`continuum checkpoint my-task --reason "pre-compact"`) while `hooks install`
  runs once and runs come and go. So `continuum precompact` resolves the run
  itself, as `observe` and `briefing` do, and records the checkpoint with
  trigger `context_pressure` — the harness-side, involuntary form of the signal
  `ContextPressurePolicy` can only see when the agent volunteers its own token
  counts. Beside the checkpoint it writes the two snapshots the guide promises,
  at the paths the guide already names, so a recipe scripted against
  `.continuum/precompact-resume.json` or `.continuum/precompact-verify.json`
  keeps working. The checkpoint also refreshes `.continuum/resume.json`, which
  is what lets the next session's SessionStart briefing detect the interruption
  without opening the database at all.

  The hook never fails its host. With no active run it exits 0 having sealed
  nothing, and a snapshot it cannot write is reported in `failures` while the
  checkpoint — the durable half, already in the hash-chained log — stands. An
  explicit `--run-id` naming a run that does not exist is still an error, since
  an operator who baked the wrong id into a hook command needs to hear it. An
  entry pasted from the guide before this landed is repointed rather than
  duplicated, because the installer uses the same empty matcher the recipe
  does.

  Codex and Gemini get no `PreCompact` entry: neither harness exposes a
  compaction event, as both guides state, and wiring a hook to an event that
  never fires would look like durability without being any. Codex observation
  stays `^Bash$|^shell$`, now pinned by a test against both pages that document
  it, so the profile and the guides fail together instead of drifting apart.

### Fixed

- Make `continuum complete` idempotent for runs that are already completed (#356).

- **Derived keys ignore surrounding whitespace in argument values (#361).**
  `render_key` substituted values verbatim, so a tool argument of `" 123 "` and
  one of `"123"` produced two different ledger keys for one resource. LLM output
  routinely carries a trailing space or newline, and with two keys the second
  call found no record of itself: the gate answered with claim instructions
  rather than the already-completed refusal, so the same side effect could fire
  twice. String values are now stripped before substitution in both `gate` and
  the enforcing `gateway`, which share one `normalize_key_value` rule so the
  hook and the proxy cannot derive different keys for the same operation.
  Non-string values are untouched, keeping templates that carry a format spec
  such as `{amount:.2f}` working. Keys are unchanged for values without
  surrounding whitespace; a run whose in-flight claim was recorded under a
  padded key will not match the stripped key derived after upgrading.

- **The gateway answers malformed JSON with 400 instead of hiding it (#323).**
  `_body` caught `JSONDecodeError` and returned an empty mapping, so a request
  whose body never parsed was carried on to key derivation and refused with
  `key template 'invoice:{id}' needs body field(s) ['id']`. That sent the
  operator to look for a field they did send, in a body the gateway never read.
  A body that is not valid UTF-8 was worse: `json.loads` decodes before it
  parses, so `UnicodeDecodeError` escaped the handler and the connection closed
  with no response at all. Both now return
  `400 {"error": "invalid JSON in request body: ..."}` before any routing or
  ledger work. A genuinely empty body still becomes an empty mapping, so a route
  whose template needs no fields stays callable with no body.

- **One list of installed hook kinds, and the kind is read off the command
  (#484).** `hooks install` duplicated the `SessionStart` briefing hook on
  every run because the installer recognised an existing entry as its own only
  when the command ended in `observe` or `gate`, while `hooks remove` already
  knew about `briefing`: the two lists were written out separately, so the kind
  added in 7c6248d reached one and missed the other. #526 fixed the
  duplication by extending the installer's list. The kinds are now a single
  `_INSTALLED_KINDS` read by both the installer and the remover, so a future
  kind cannot be added to one and forgotten in the other, and the kind is
  derived from the command being installed rather than checked as a set: an
  install of one kind can no longer repoint an entry of another that happens
  to share the event and matcher, which would drop a hook the caller never
  named. A command this module did not build carries no kind and is appended,
  as before. A settings file that already holds duplicates is cleaned by
  `hooks remove` followed by `hooks install`.

- **`continuum tree --limit` truncates the child list instead of being ignored
  (#321).** The flag was registered with `help=argparse.SUPPRESS` and never
  read, so `--limit 5` was accepted and printed every child anyway: an operator
  with a wide family got the full wall of output and no error saying why. The
  flag now shows the newest `n` children and reports what it hid
  (`... 3 of 40 children hidden by --limit 5`), because a truncated tree that
  says nothing is indistinguishable from a small family. `--json` gained
  `children_total` and `children_hidden` alongside the truncated `children`
  list; output without `--limit` is unchanged. The truncation is display-only:
  `children_of` still returns every child, so the family safety roll-up behind
  `resume` cannot lose an uncertain child that scrolled off the printed list.
  A limit below `1` is refused with exit code 1 rather than clamped, since
  `--limit 0` would render a family with children as childless.

## [0.1.0] - 2026-08-27

### Added

- **Wheel artifacts on every push to main (#279).** Wheels were built only on
  release tags, so testing current `main` meant cloning it. The CI workflow
  gains a `build-wheel` job that runs on pushes to `main` in the canonical
  repository: `uv build`, then a smoke test that installs the fresh wheel into
  a clean virtualenv and runs `continuum --version` before the `dist/` output
  is uploaded as a workflow artifact. Tag and release behaviour is unchanged.

- **Semantic replay-or-fork at the tool boundary (#291).** Exact key
  matching fails when an LLM renders the same intent with different argument
  text. New `replay_similarity` module adds three comparison backends (exact,
  fuzzy token-set Jaccard, pluggable embeddings) and a `classify_call`
  function that classifies post-restore calls against prior completed actions
  of the same type. Above the replay threshold: return cached result.
  Between thresholds: divergent, require fork. Below: fresh claim. Cross-type
  matching is never performed. This implements ACRFence's proposed-but-unbuilt
  defence (arXiv:2603.20625) on top of CONTINUUM's existing gate + ledger.

- **Distribution without PyPI: Docker image, Codespaces, and git installs.**
  A `Dockerfile` publishes a slim image whose default command runs the
  crash-recovery demo end to end (`docker run --rm ghcr.io/cyrax321/continuum`)
  and whose entrypoint override exposes the CLI; CI builds and pushes it to
  GHCR on pushes to main and release tags (`.github/workflows/docker-publish.yml`,
  with a `continuum --version` smoke test). A `.devcontainer/devcontainer.json`
  gives one-click GitHub Codespaces with the dev toolchain preinstalled
  (port 8765 forwarded for the dashboard). The README Quick Start gains a
  zero-setup table: docker run, `uvx`/`pipx run` straight off the git URL,
  and pip/uv installs from git or a release-attached wheel. The PyPI publish
  job in the release workflow is now opt-in via the `PUBLISH_PYPI`
  repository variable, so tagging no longer fails while trusted publishing
  is unconfigured; wheels still land on the GitHub Release.

- **Multi-agent hierarchies: parent/child runs, aggregated contracts,
  A2A identity (#243).** `continuum start --parent <run_id>` attaches a
  child run to its supervisor (validated: parent must exist and not be
  completed; recorded in a new runs.parent_run_id column, schema v6 with
  index). The parent's resume composes the family's worst state: no RESUME
  while any non-terminal child holds uncertainty or requires review - the
  most cautious signal wins, house-style. Resume JSON gains family_rationale
  and children arrays naming exactly which child blocks and why.
  `continuum tree <parent>` renders the hierarchy with per-child recovery
  states. A2A task ids ride on run metadata via `start --a2a-task`, giving
  external agent-to-agent handoffs durable identity without claiming full
  protocol support. Siblings share nothing mutable: coordination stays in
  the ledger and contracts.

- **Citation audit for external-report references (#261).** An external
  Gemini-generated survey pitching a "CONTINUUM paradigm" cited works absent
  from our verified related-work list. Each candidate ID was resolved against
  the arXiv API on 2026-08-24 and the verdicts recorded in
  `references/citation-audit-2026-08-24.md`: Belayer (arXiv:2608.14635) is real
  but RL-training-scoped, so out of scope here; the transactional sandboxing
  paper (arXiv:2512.12806) is real with its 14.5% overhead figure confirmed;
  ReliabilityBench (arXiv:2601.06112) is real and accurately described, and is
  the model for the #258 stress surface; DAPH exists only as a self-published
  Medium post and its "(ICLR Workshop)" attribution has no supporting record,
  so it stays uncited. A full-text pass additionally proved the report's
  "10.6% task completion" figure for the sandboxing paper appears nowhere in
  that paper, and quoted ACRFence's own conclusion ("does not yet include an
  implementation of ACRFence itself") against the report's inverted claim of
  a 10/10 defence success rate. The report's unsourced statistics are documented as
  corrections. Two verified-relevant papers joined the README related-work
  list with abstract-backed descriptions.

- **Human-in-the-loop surface on the dashboard (#242).** request_human
  walls finally have a door with an audit row: the run page renders buttons
  for confirm (REVIEW_CONFIRMED, Origin.HUMAN), reconcile
  occurred=true/false through ActionLedger.reconcile, and complete (run row
  flipped to COMPLETED) whenever a run is blocked or has uncertain actions.
  Mutating POST endpoints are fail-closed - refused until
  CONTINUUM_DASHBOARD_TOKEN is set - and every action maps 1:1 onto the
  human CLI verb, landing identical event types and provenance. Reads stay
  open; unknown routes 404.

- **Version pinning on claims and summaries (#241).** Replay correctness
  needs environment identity: which prompt version, tool schema and model
  produced a decision (prompt-migration hazard, arXiv:2507.05573; Zylos
  survey lists pinning as part of replay correctness). New closed-set
  `pinning` dict - prompt_sha256, tool_schema_sha256, model_id,
  policy_version - accepted by `continuum intercept_action` and
  `continuum_record_summary`, stored verbatim with EXTERNAL_AGENT provenance
  on the ACTION_RECORDED STARTED record / summary payload. Values are
  validated (known keys only, 256-char cap: store the hash, not the
  artefact). `continuum resume --pinning '<json>'` diffs caller-supplied
  pins against the newest recorded set and surfaces drift as informational
  lines in text and JSON (`pinning_drift`) - degrade, never block. Pure
  helpers live in `continuum.pinning`.

- **Run-level retry budgets (#240).** Agent loops invent retries: a failing
  upstream gets hammered because the model re-plans after every failure, and
  each attempt opens a fresh ledger slot. New `.continuum/budgets.json`
  registry caps attempts per action type (with a default fallback); every
  ACTION_RECORDED claim slot counts as one attempt, so retries under the
  same key still consume budget. `continuum intercept_action` refuses claims
  beyond the budget with an instructive error, and new read-only command
  `continuum budget <run>` reports attempts/max/remaining per type.
  `backoff_delay` ships as a pure helper (exponential + cap; jitter is the
  caller's job) because CONTINUUM never performs retries itself - it counts
  and gates them.

- **Event-log compaction (#239).** `continuum compact <run>` bounds live-log
  growth for month-long runs: it appends an EVENT_LOG_ANCHORED marker, moves
  the pre-anchor prefix verbatim into a new `events_archive` table (schema
  v5, SQLite + Postgres), and leaves the live chain append-only with the
  anchor as its trusted genesis. verify walks anchored logs natively;
  resume/replay fold from the restored checkpoint plus post-anchor tail; and
  archived rows remain digest-auditable for deep checks. The anchor is
  created fresh at compaction time (forced checkpoint) because anchoring at
  an ancient version would leave almost everything in the live log. Payload
  offloading to blob storage is split into follow-up work.

- **Production server mode (#238).** Three pieces: (1) CI now runs a real
  Postgres 16 service container against the Postgres contract tests, and the
  contract tests themselves were rewritten against the modern surface: the
  originals predated strict enum validation and Goal models, passing raw
  strings that today's pydantic models rightly refuse - they had rotted in
  skip-guaranteed obscurity. The gateway backfill SQL was also fixed to
  Postgres jsonb operators (it previously used SQLite-only `json_extract`). (2) `continuum serve --transport http`
  exposes the full sidecar dispatch over POST /<method> JSON for non-Python
  agents: same handlers, same token auth (`CONTINUUM_SERVE_TOKEN`), errors
  mapped to status codes (404 unknown method, 403 unauthorized, 400 bad
  params, 500 storage failures) without killing the server. (3) Honest
  scoping note: duplicate reconciliation between concurrent workers is
  already impossible by construction (ledger claims commit inside IMMEDIATE
  transactions); run-level exclusivity leases remain available to adapters
  via LeaseCoordinator rather than being forced onto every surface.

- **Replay-safety guard as a portable primitive (#237).** The gate's
  decision table is extracted into `continuum.replayguard.evaluate`, a pure
  core over the folded ledger that the gate now delegates to (single source
  of truth). On top of it: `protected_call` executes a side effect at most
  once per stable key and returns the journalled result on replay, raising
  `ReplayBlocked` for uncertain or unclaimed states; and
  `langgraph_protected_node` wraps LangGraph nodes so interrupt/crash
  replays become cache hits instead of double-fired side effects - closing
  the re-execution window LangGraph documents (issue #6208; ACRFence
  arXiv:2603.20625). A chaos-matrix test encodes the crash points from the
  durable-execution survey as executable assertions.

- **Native LangGraph checkpointer (#236).** CONTINUUM now implements
  LangGraph's BaseCheckpointSaver over its own storage
  (`make_continuum_checkpointer(storage)`), so production LangGraph apps keep
  their native persistence API while gaining the hash-chained event log,
  provenance tagging, and everything else CONTINUUM provides. thread_id maps
  deterministically to a run (`lg-<thread>`); every put lands a
  STATE_CHECKPOINTED event with EXTERNAL_AGENT provenance; channel values
  round-trip through LangGraph's JsonPlusSerializer (pydantic models,
  datetimes). Schema v4 adds the two lg_* tables (additive migration plus
  baseline DDL). Seven tests cover put/get round trips with typed values,
  newest-first listing with limit/before/metadata-filter, parent chains and
  point-in-time gets, pending writes, total thread deletion, per-put
  provenance events, and a real StateGraph resuming across separately built
  graph instances.

- **Reasoning-context rehydration (#235).** Task-state recovery without
  cognitive-state recovery produces sessions that are safe yet amnesiac.
  New mutating MCP tool `continuum_record_summary` stores a bounded,
  self-authored summary of where the agent's reasoning stands - plan stack,
  decisions with rationale, open questions, working set - hard-capped at
  4096 serialized characters so it can never become a transcript dump.
  Summaries land as REASONING_SUMMARY events with EXTERNAL_AGENT provenance
  and are strictly informational: they never move mode or safety. The
  SessionStart briefing serves the newest summary verbatim ("where the last
  session left off"), so a fresh session inherits the dead session's plan
  instead of guessing from a progress bar.

- **README refresh.** The README predated this week's work in every
  direction users touch first: the Quick Start now leads with the two-minute
  harness-wiring path (start a run, `hooks install`, no CLAUDE.md); the
  Features table gains nine rows (gate, briefing, observations, reconciler
  probes, executable guidance, gateway, OTel bridge, action index);
  Framework Integration documents the CrewAI/AutoGen/Pydantic-AI thin hooks
  and the gateway/OTel fallback seams; the Roadmap marks the dashboard and
  the enforced-durability work complete; test counts are current (1224).

- **Gateway hardening and docs refresh.** The enforcing proxy now refuses
  request bodies above 10 MB with 413, draining (without buffering) up to a
  256 MB sanity bound so clients finish sending and read the refusal instead
  of dying on a broken pipe - a proxy that reads unbounded bodies into memory
  is a denial-of-service surface against the agent it protects. The CLI
  reference gains this week's commands plus an "executable configuration"
  warning: the `.continuum` registries reference commands that run with your
  user privileges and deserve the same scrutiny as cloned test scripts.

- **`continuum complete <run>`: close a run from the keyboard.** Found
  missing during live testing: MCP-driven runs close via adapter events, but
  there was no CLI way to finish a run, so finished work kept surfacing as
  the active run and hijacked every fresh session's resume. The command
  appends `REVIEW_CONFIRMED` plus `RUN_COMPLETED` (both human-sourced, so
  self-certification gates clear) and flips the run row to COMPLETED, with
  an optional `--summary` note. Terminal runs can never be offered for
  resume again; double-clicking is harmless; unknown runs exit NOT_FOUND.

- **Enforcing HTTP gateway (seam 4 of the universality roadmap, #213).**
  The last blind spot no harness hook can see: outbound HTTP calls made from
  agent code in any language. `continuum gateway` runs a local enforcing
  proxy; routes are registered in `.continuum/gateway.json` (host, methods,
  path prefix, action type, key template over JSON body fields). Decision
  semantics mirror the gate exactly: a matching request forwards to the real
  upstream only when a live STARTED ledger claim exists for its derived key;
  duplicates are refused because the effect already happened; unknown
  outcomes demand reconciliation. After forwarding, the gateway settles the
  claim itself - COMPLETED on 2xx/3xx with TOOL_COMPLETED evidence, FAILED-
  certain on upstream 4xx, FAILED-uncertain on 5xx and network errors (the
  effect may still have landed) - all inside the run's hash chain. Unknown
  hosts are refused fail-closed: a proxy that silently forwards anywhere
  would be an open relay wearing CONTINUUM's name.

- **Thin adapters for CrewAI, AutoGen and Pydantic AI (seam 1 extension,
  #213).** Three more production frameworks get the durability seam with
  their own verified interception surfaces, all routed through one shared
  `ContinuumToolGuard` over `ActionLedger`: CrewAI's global before/after
  tool-call hook registry (with an action-type filter and a working
  uninstaller), AutoGen's `FunctionTool.run_json` wrapped in place (agent
  construction code unchanged; failures recorded then re-raised), and Pydantic
  AI's async Hooks capability (`before_tool_call`/`after_tool_call` matching
  the documented protocol) registered via `capabilities=[...]`. Keys follow
  the MCP contract - resource identity via `key_fn`, argument-hash default -
  so exactly-once survives model drift. Framework imports stay lazy; every
  surface is tested against duck-typed stand-ins, no SDK required.

- **OpenTelemetry bridge (seam 5 of the universality roadmap, #213).**
  Production stacks already emit OTel; now those spans become CONTINUUM
  evidence with zero framework cooperation. `make_span_processor(storage)`
  returns a standard SpanProcessor to register on any TracerProvider: ended
  spans carrying a recognised tool-name attribute (`gen_ai.tool.name` per the
  GenAI semantic conventions plus common vendor spellings) are mirrored into
  the active run's hash-chained log as `TOOL_COMPLETED`/`TOOL_FAILED`,
  EXTERNAL_AGENT provenance, identical in shape to hook observations. The
  pure core (`observation_from_span`, `record_span`) is duck-typed and
  dependency-free; the SDK import is lazy with an actionable install hint,
  and the bridge ships behind the new `otel` extra. This covers frameworks
  CONTINUUM cannot wrap or hook - Rust/Go/TS agents, internal platforms -
  wherever they emit traces.

- **Session briefing: state without CLAUDE.md (#213 ergonomics follow-up).**
  The last two voluntary behaviours depending on per-repo prose were
  resume-on-start and knowing the active run at all. New read-only command
  `continuum briefing` prints exactly what a returning agent needs - active
  run, goal, progress, recovery verdict, executable next steps and recent
  disk-checked observations - as SessionStart-compatible context JSON.
  `hooks install` now wires it alongside observe on every supported client's
  session-start event (Claude Code, Gemini CLI, Codex), so a fresh session
  learns its durable state from deterministic injection instead of a prompt
  file. With no active run it says how to create one.

- **Actionable recovery guidance (`human_steps`).** The contract named what
  was blocked but not what to do about it, leaving every resuming agent to
  translate `reconcile_action:abc` into commands by hand. Resume and
  validate now render executable next steps derived from the plan plus live
  project automation: a reconcile step with a registered probe becomes one
  `continuum reconcile <run>` command; without one it names the external
  check and the exact `continuum_reconcile_action(...)` call, and says why
  it is manual; human review points at `continuum confirm`; dependency
  steps name the `--env` re-pin. Gate presence is surfaced as protocol
  guidance. Steps flow through CLI text/JSON, MCP `continuum_resume`, and
  the sidecar's mirrored payload. Guidance is rendering only: derived from
  existing state and config, never executed by CONTINUUM itself.

- **Client installers for Gemini CLI and Codex CLI (#209).** The observe and
  gate commands were already client-agnostic; wiring them into new clients is
  now data, not code. `CLIENT_PROFILES` describes each client's settings
  path, hook event names and tool matchers, and `continuum hooks
  install|remove` accepts all three clients: claude-code (PostToolUse/
  PreToolUse on Write|Edit), gemini (AfterTool/BeforeTool on
  write_file|replace, per the official hooks reference) and codex
  (PostToolUse/PreToolUse, Bash-only today because Codex's documented hook
  surface does not traverse apply_patch or MCP tools). Removal scans every
  event list rather than hardcoded names, so it works across clients while
  still touching only entries this tool installed. The installer surfaces
  Codex's `[features].codex_hooks = true` requirement as an explicit hint
  instead of hand-editing TOML. A regression test pins that each client's
  default settings path comes from its profile, closing a gap found live:
  every earlier test passed explicit paths, so a hardcoded CLI default was
  silently writing all clients' hooks into Claude Code's settings file.

- **Pre-action gate: host-enforced side-effect claims (#217).** The two-phase
  action protocol was a convention the model was asked to follow; nothing
  stopped an unclaimed side effect from firing, which degrades exactly-once
  to at-least-once-with-nothing. `continuum gate` is a pre-tool-use hook that
  makes the protocol physical: a call whose tool is registered in
  `.continuum/gate.json` may proceed only when a live ledger claim already
  exists for its derived key. Keys come from configuration templates
  (`{"tools": {"send_invoice": {"key_template": "invoice:{customer}:{id}"}}}`
  applied to the call's structured arguments), never from LLM-authored
  strings. The decision table mirrors the ledger exactly: no claim denies
  with instructions to route through `continuum_intercept_action`; a
  COMPLETED record denies as a duplicate (the dedup verdict made physical);
  UNKNOWN denies with reconcile instructions; closed attempts must be
  reclaimed; only STARTED passes. Exit 2 feeds the reason back through the
  harness so the model can comply on retry. `hooks install claude-code
  --with-gate` wires PreToolUse alongside PostToolUse observe: claim before,
  evidence after, both outside model control. Stated limitation: v1 gates
  structured tool surfaces only, not shell commands run inside Bash.
  Verified live over the real stdio MCP boundary: deny, claim via MCP,
  allow, complete, duplicate denied, observation recorded in one hash chain.

- **Lazy adapter imports (#214).** `continuum.adapters` sits on the critical
  path of every entry point, but eagerly imported the optional SDK adapters,
  so opening the MCP server cost roughly 3s before answering its first
  request, and every `continuum observe` hook subprocess paid it again. The
  dependency-free adapters stay eager; browser/container/kubernetes and the
  langchain/langgraph/openai names now resolve through module
  `__getattr__` (PEP 562) on first access, in both `continuum.adapters` and
  the top-level `continuum` package. The public import surface is unchanged.
  Measured on this machine: MCP server spawn-to-first-response drops from
  about 3s to about 0.1s, and importing either package leaves none of
  langgraph, langchain or openai in `sys.modules`. The full test suite also
  gets faster for the same reason. Tests in `tests/test_adapters_lazy.py`
  run their key assertions in subprocesses so earlier imports cannot mask a
  regression.

- **Action index: indexed cross-run idempotency lookups (#216).** Unscoped
  claims folded every other run's complete event log on a local miss,
  O(total logged events) per lookup. Schema v3 adds `action_index`, a derived
  projection of the `ACTION_*` events (one row per ledger key), maintained
  inside the same transaction as each event insert and backfilled from
  existing events by the migration, so v2 databases gain correct lookups on
  open. `ActionLedger` uses it whenever the engine provides one
  (`Storage.supports_action_index`) and keeps the historical scan as the
  fallback for engines without it; verdicts are identical because both read
  the same log semantics. The log remains the source of truth:
  `continuum verify --index` compares the projection against the fold and
  reports drift, `--repair-index` rebuilds rows from events. Measured at 300
  runs: foreign lookup ~19 ms scanning vs ~0.015 ms indexed, and the scan
  cost grows linearly with store size while the index does not. Tests in
  `tests/test_action_index.py` pin incremental maintenance across claims,
  completions and reopens, equivalence with the scan for completed,
  duplicate and uncertain-elsewhere cases, scoped-key isolation, drift
  detection and repair, the engineless fallback path, and v2 backfill.

- **Reconciler registry: probes settle uncertain side effects (#218).** An
  uncertain action blocked resume until a person checked the external
  system by hand, which does not scale past the first high-volume run.
  Projects can now register one probe per action type in
  `.continuum/reconcilers.json`; the probe receives the Action record as
  JSON on stdin and prints a verdict (`occurred=true|false|unknown` or a
  JSON object). `continuum reconcile <run>` runs the registered probes over
  every pending action: definitive verdicts are applied through the ledger
  and land as `ACTION_RECONCILED` events sourced `DETERMINISTIC` (local,
  registered, auditable); probe errors, timeouts, unparseable output and
  explicit unknowns leave actions untouched; types without a probe are
  skipped. Auto-settlement therefore only shrinks the human queue, never
  widens what an agent may certify itself. `--dry-run` reports without
  writing. The command is deliberately separate from validate/resume so
  those stay read-only under the exit-code safety contract. Verified live:
  a claim committed then the MCP server killed leaves `REQUEST_HUMAN`;
  after registering an outbox-checking probe, `reconcile` settles the
  action and resume returns `RESUME`. Tests in `tests/test_reconcilers.py`
  cover verdict parsing, the settle table, failure isolation, dry-run,
  provenance and exit codes.

- **Post-checkpoint observations surfaced in the recovery contract (#208).**
  The observation hooks (#210) recorded what landed on disk, but a resuming
  session had to know to inspect the raw event log to see it; the contract
  reported self-reported progress alone. `RecoveryContract` now carries
  `post_checkpoint_observations`: every file observation recorded after the
  latest state version's source sequence, newest first and capped at 50 with
  an explicit truncation marker, each disk-checked at assess time as
  `verified`, `changed`, `missing` or `recorded`. The rows appear in
  `continuum resume`/`validate` output and every rendered contract.
  Deliberately informational: provenance stays conservative per #207, so
  observations never move `mode`, `safe` or any required action; they tell
  the resuming agent what is true about the workspace without certifying
  anything. Verified live: an intact artifact shows `verified`, one modified
  after its observation shows `changed`, and the decision fields are
  byte-identical to a run without observations.

- **Host-side observation hooks close part of the durability gap (#207).**
  Recovery depends on the agent voluntarily calling the recording tools, so a
  kill between performing work and reporting it left the next session a
  contract that understated what had landed on disk (observed live with Claude
  Code on 2026-08-22: an artifact fully written, progress still 0/1, zero
  checkpoints). Two new CLI commands convert that voluntary recording into
  mandatory interception for Claude Code: `continuum observe` reads one
  PostToolUse hook payload from stdin (or `--payload-file`) and appends a
  `TOOL_COMPLETED` event carrying the tool name, mutated path, byte count and
  SHA-256 of the file as it exists right now; `continuum hooks install
  claude-code` wires file-mutating tool completions to it by editing
  `.claude/settings.json` in place (preserving unrelated settings,
  self-healing a stale baked-in binary path, refusing to touch a settings file
  that is not valid JSON), with `continuum hooks remove claude-code` to undo.
  Observations target the explicit `--run-id`, else the most recently active
  non-terminal run; with no run active they are dropped with exit 0 so hooks
  never disturb unrelated sessions. Events are sourced `EXTERNAL_AGENT`: they
  are evidence a resumed session can weigh, not a laundering path to trusted
  state. Tests in `tests/test_cli_observe.py` include driving the exact
  command string baked into settings.json through a real shell.

- **`scoped_to_run=False` now enforces global uniqueness (#34).** An unscoped
  idempotency key claims store-global identity but the ledger only replayed its
  own run's log, so two runs could each open a fresh slot for the same
  identity. `claim` now scans every other run in the store when an unscoped
  claim misses locally: a completed record anywhere deduplicates the claim
  (`fresh=False` with the stored result), and an unresolved attempt in another
  run raises `UnknownSideEffect` rather than opening a parallel slot, since a
  foreign record cannot be reconciled from this run's ledger. Certain failures
  and compensations elsewhere do not block. The scan is event-sourced through
  a shared `fold_action_events` helper so both ledgers read the log with
  identical semantics; it is paid only on the unscoped path after the local
  lookup missed. Regression tests cover cross-run dedup, the uncertain-elsewhere
  refusal, and the certain-failure pass-through.

- **Automatic durability for every harness (#191).** The file-derived progress
  hook and the policy-gated background checkpoint are now wired into
  `GenericAgentAdapter` itself instead of living only in examples. With the
  opt-in `auto_file` / `auto_total` constructor arguments set, every completed
  `intercept_action` mirrors file-derived progress into the log (gated on the
  derived count actually changing) and submits a checkpoint write to a shared
  background executor only when the checkpoint policy agrees, so the agent's
  turn never blocks on SQLite I/O. `LangChainAgentAdapter`,
  `LangGraphAgentAdapter` and `OpenAIAgentAdapter` forward both options to the
  base class, making the same zero-prompt durability available from all three
  frameworks. Supporting fixes in `continuum.hooks`: one shared executor with
  an `atexit` shutdown replaces the per-hook thread pool that leaked a worker,
  the async hook now returns whether the policy actually warranted a write
  instead of always True, `record_file_progress` appends `TASK_UPDATED` plus
  the `EVIDENCE_ADDED` tail only when the count changed, and the adapter path
  delegates to these hooks rather than duplicating them inline. Regression
  tests in `tests/test_harness_auto.py` cover tail evidence through the
  adapter path, no log bloat over unchanged files, non-blocking auto progress
  after an intercepted action, and subclass forwarding.

- **Localized recovery is now dep_scope-aware and file-aware (#184).**
  `RecoveryEngine.assess` respects `Action.dep_scope` when a scope is given: an
  uncertain side effect tagged to a dependency outside the scope no longer
  blocks that scoped decision, while untagged actions stay blocking. Passing the
  source-level `DependencyGraph` to `assess`/`assess_scoped` surfaces every file
  importing a scoped dependency as `RecoveryDecision.impacted_files`. Phase 6
  gains an `out_of_scope_side_effect` scenario covering both paths.

- **Leftover issue sweep (Phases 2, 3, and provenance).** Closed the remaining
  open issues from the master plan with working, tested code:
  - Source-level `DependencyGraph` (`continuum.analysis.depends`): reads
    `pyproject.toml`/`requirements.txt` and parses `import`/`from` via the stdlib
    `ast` module; `owner_of`/`files_using` plus stdlib vs third-party distinction
    (#100). Degrades gracefully when no manifest is present (#109).
  - `dep_scope` on `Action`, threaded through `ActionLedger.claim` and
    `AgentAdapter.intercept_action`, so operations can be tagged with the
    dependency they belong to (#103). (The plan's `OperationContext` does not
    exist in the current architecture, so the intent was applied to `Action`.)
  - `AdapterAction`/`AdapterResult` and `run_action`, a uniform facade over
    `AgentAdapter.intercept_action` so recovery treats every adapter the same
    (#112).
  - Environment adapters behind `AgentAdapter`: `FilesystemSandboxAdapter`
    (CI-safe default, #160), `PythonInProcAdapter` (CI-verified, #116), and
    guarded `ContainerAdapter` (#116), `BrowserAdapter` (#158, playwright), and
    `KubernetesAdapter` (#159, kubectl). The latter three import their optional
    dependency lazily and skip smoke tests when it is absent.
  - `continuum status --provenance` renders the canonical provenance projection
    per state item (#148).
  - Benchmark scripts `benchmarks/localized_repair.py` (#106) and
    `benchmarks/graph_build_overhead.py` (#111); both are synthetic and make no
    external-world claims.
  - CLI scoped-recovery smoke test and recovery-policy regression tests
    (#110, #104).

- **Recovery benchmarking and correctness scenarios (Phase 6).** The existing
  CONTINUUM-Bench (`src/continuum/benchmark/__init__.py`) already provides the
  harness, metrics schema, baseline strategies (continuum, replay,
  naive-checkpoint), and report. Phase 6 adds `continuum.benchmark.phase6`: 12
  recovery-correctness scenarios (dependency corruption, ledger tamper, lease
  exhaustion, checkpoint rollback, concurrent safety, adapter failure, and more)
  driven by a tiny timing harness that writes JSON and Markdown reports.
  `benchmarks/run.py` runs the suite; `tests/test_phase6.py` covers it. This
  turns the Phases 1-5 guarantees into observable, reproducible evidence.

- **Recovery ledger: append-only, tamper-evident, reconcilable (Phase 5).**
  `continuum.recovery.ledger` adds `RecoveryLedger`, a durable audit record of
  recovery decisions. Entries are hash-chained (tamper-evident, `verify` reports
  the last trusted index), `compact` drops old entries while preserving anchors
  and re-sealing the chain, `record_gate` / `pending_gate` persist a
  human-in-the-loop decision, `record_attempt` / `requires_human` enforce a
  recovery attempt budget before escalating to a human, and `reconcile` detects
  state-vs-ledger drift. The ledger takes an optional `LeaseCoordinator` for
  cross-process safety and ships `MemoryLedgerBackend` (tests) and
  `FileLedgerBackend` (JSONL). Tests in `tests/test_recovery_ledger.py`.

- **Automatic checkpointing: recovery anchors, anchor lookup, and pruning (Phase 4).**
  `CheckpointTrigger.RECOVERY` marks a checkpoint taken because a recovery
  decision judged the run unsafe to continue from. `CheckpointManager` gains
  `checkpoint_on_recovery` (the explicit hook to call after a non-RESUME
  decision), `last_recovery_anchor` (lookup the newest recovery anchor, optionally
  before a version), and `prune` (drop old checkpoints while keeping the newest
  `keep` and preserving RECOVERY anchors). `Storage.delete_checkpoint` is added to
  the ABC and implemented for SQLite and Postgres, and `StateCheckpoint` gains an
  optional `reason` field so an anchor is self-describing. The recovery engine
  stays read-only; auto-checkpointing is an opt-in call, not a hidden mutation.
  Tests in `tests/test_checkpoint_phase4.py`.

- **Portable interchange format (B4).** New `continuum.interchange` package turns
  durable output into a versioned, self-validating JSON envelope so external
  tools can read and verify CONTINUUM without embedding Python. `export_*` /
  `import_*` cover `SemanticState`, `RecoveryContract`, and `RecoveryDecision`
  (lossless round-trip, since every `RecoveryDecision` field is a pydantic
  model), `published_schema` returns the JSON Schema an external verifier can
  check, and `dump_payload` / `load_payload` handle files. Canonical example
  artifacts live in `examples/interchange/`. Stdlib-only beyond the existing
  pydantic dependency; tests in `tests/test_interchange.py`.

- **Forward schema migration (B2.1).** `SQLiteStorage` no longer refuses every
  older database. A new `continuum.storage.migrations` runner seeds a fresh
  database at `SCHEMA_VERSION`, forward-migrates a one-step-behind database
  (applying each registered, additive migration and recording it in a
  `schema_migrations` table), and still raises `SchemaVersionError` for a
  database written by a newer build or for an older shape with no registered
  path. The first migration (`v2`) adds the `versions` table and the per-event
  `source` / `prev_hash` provenance columns. Tests in
  `tests/test_storage_migrations.py`.

- **Lease / distributed-lock coordinator (B2.2).** New `continuum.concurrency`
  package guarantees "one agent resumes one run": a `LeaseCoordinator` ABC with
  `acquire` / `renew` / `release` / `holder`, an `InMemoryLeaseCoordinator`
  (single process / tests) and a `SQLiteLeaseCoordinator` over a dedicated
  sidecar database so separate processes coordinate through the filesystem.
  Leases are short-lived and renewable, and an expired lease is reclaimable by
  another holder. Tests in `tests/test_lease.py` cover the shared contract,
  expiry, cross-process contention, and a fuzz loop over many runs.

- **PostgreSQL storage backend (B2.3).** `open_storage` now routes
  `postgresql://` / `postgres://` URLs to a new `PostgresStorage`
  (`continuum.storage.postgres`), a synchronous `psycopg` implementation of the
  full `Storage` contract, so multiple agents or `continuum serve` sidecars can
  share one durable store. `psycopg` is pulled in via the optional `[postgres]`
  extra; without it, opening a Postgres URL fails clearly
  (`RuntimeError: ... psycopg ...`) instead of silently falling back to SQLite.
  Sequence allocation relies on the same `UNIQUE` constraints as the SQLite
  engine. `tests/test_storage_postgres.py` exercises the contract but skips
  cleanly when `CONTINUUM_TEST_POSTGRES_DSN` is unset or `psycopg` is absent, so
  it runs for real in CI against a Postgres service. **Local note:** this slice
  is unverified against a live Postgres here (no server / driver in this
  environment); it type-checks and its tests skip, matching the plan's "skip
  locally, run in CI" model, and should be validated in CI before relying on it.

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

- **Dashboard bind testability (#270).** Server construction is split into
  `make_dashboard_server(storage, port, host)` so the bind address is a
  plain function of its arguments; `serve_dashboard` closes the listening
  socket on shutdown. Three new tests pin the loopback default, honour an
  explicit `0.0.0.0`, and smoke GET / over a real socket.

- **Seven-level testing guide (references/testing.md, #234).** New contributor-facing docs organize verification into seven escalating levels, from automated suite to live gateway, crash harness, benchmark and chaos matrix, so every seam has a reproducible test path. No runtime change.

- **Fork semantics: audited divergent continuations at the tool boundary (#259, #286).** Completes the replay-or-fork triad. A post-restore call whose intent genuinely diverges from any journalled intent is now surfaced as a fork candidate with nearest neighbours. An approved fork records a `RUN_FORKED` event on the parent log and creates a linked child run with its own ledger frontier, both verifiable. This is the third outcome alongside replay (cache hit) and gate reject.

- **Informed retry: engine-authored prior-attempt summaries (#265, #275).** After a non-trivial recovery, `continuum resume`, `continuum briefing` and the MCP and serve contracts include a bounded, engine-authored summary of what failed, what changed and what to avoid. The summary is deterministic (derived from validator findings, ledger reconciliations and planner steps), capped at 4 KB, informational only, and rides the hash chain. This implements the AgentRewind-informed retry loop without conflating agent-authored and engine-authored summaries.

- **Consumed-grant tracking, authority-resurrection denial (#269, #287).** Single-use authorization grants can now be registered on `continuum_intercept_action` and the adapter claim path. Completing or failing the action marks the grant spent, and a post-restore claim that tries to reuse a grant whose consumption sequence is after the restore point is rejected with a dedicated reason and audit event. This closes the Authority Resurrection class from ACRFence alongside the replay half.

- **Months-scale upgrade spec and live web synthesis (#339).** New docs `docs/UPGRADE_SPEC.md` plus supporting synthesis (`docs/ARCHITECTURE_EVOLUTION.md` section 19) and `STATUS.md` checklist make the months-long agent plan reviewable without re-deriving it. Docs-only, no runtime change.

- **Authorization-bound budget registry (#390, #411, #424).** `.continuum/budgets.json` gains an optional `authorization_bound` map, validated on load, plus pure helpers for per-type and per-key budget evaluation. The registry is strictly additive and prepares the schema for the authorization-bound budgets track. No behaviour binds to it yet, so existing runs are byte-identical.

- **Constraint pinning events with hash-only payloads (#391, #416, #425).** Two new event types `CONSTRAINT_PINNED` and `CONSTRAINT_RETRACTED` carry SHA-256 digests of constraint text and never the text itself. They are emitted through the normal event path, survive compaction and context reconstruction, and prepare the constraint-verified recovery track. Payloads are hash-only by construction.

- **Pure precondition derivation over event prefixes (#389, #406, #428).** New read-only module `continuum.recovery.precondition` derives, from any event prefix, the preconditions an edit must satisfy (dependence results judged by completion inside the span, not just presence). The derivation is pure, deterministic and never mutates the log, and is the precondition half of the recovery and fork path.

### Changed

- **Editable-install troubleshooting (#402).** `CONTRIBUTING.md` now explains
  that moving or renaming a clone leaves the old path in editable-install
  metadata and gives uninstall/reinstall commands to refresh it. `STATUS.md`
  records the clean-venv result confirming the package configuration already
  resolves the current repository correctly.

- **`continuum dashboard` binds 127.0.0.1 by default** (#270); pass
  `--host 0.0.0.0` to opt into network exposure. The previous
  all-interfaces bind exposed recovery contracts (goals, side-effect
  arguments and results, event payloads) unauthenticated to the local
  network.

- **MCP docs: the `CONNECTION_CLOSED` failure mode, and the eleventh tool.**
  `docs/api/mcp.md` promised that with `.mcp.json` present "Claude Code registers
  the server automatically". That holds only when the environment CONTINUUM was
  installed into is on the `PATH` the client inherited. When it is not, the client
  cannot spawn `continuum-mcp` at all and reports the failed spawn as
  `CONNECTION_CLOSED` - a message that reads like a server that crashed but
  describes an executable that was never found. No CONTINUUM code runs in that
  state, so the server cannot detect, report, or recover from it, and the whole
  diagnosis has to happen client-side. The registration section now states what a
  bare command name actually requires, and a new Troubleshooting section carries
  the diagnosis (`which` / `where.exe`, then `--help` through the full path) and
  two remedies: launch the client from the activated environment, or pin the
  absolute path with `claude mcp add --scope local`. The second also documents the
  conflicting-scopes diagnostic it produces and warns against
  `claude mcp remove continuum-mcp -s project`, which edits the committed
  `.mcp.json` and unregisters the server for every other clone. Registration is no
  longer described as instrumenting anything on its own: state is recorded when
  the agent calls the tools, or when the `PostToolUse` hook records a write
  outside the model's control.
- **MCP tool count corrected to eleven.** `continuum_record_summary` (#235)
  shipped with a CHANGELOG entry but never reached the reference docs. It is now
  in the `docs/api/mcp.md` tool table, and the count is corrected from ten to
  eleven - three read-only, eight mutating - in `README.md`, `docs/api/README.md`,
  `docs/research/token_floor.md`, `references/mcp.md`,
  `references/auto-resume-integration.md`, and `references/testing.md`.


- **README.** `Contents` laid out as a horizontal wrapping nav; Security
  Extension added to the Features table and table of contents; website link
  points to the live Vercel site; `How it works` diagram
  (`docs/assets/architecture.svg`) replaced with a complete view that includes
  the Security Extension.
- **CI.** `ruff` pinned to `0.16.2` and `ruff format` applied, so the lint
  job's format-check is reproducible (it had been failing on unpinned ruff).

- **README and STATUS.** Documented the current project structure as a module
  map (LOC per layer) in the README Architecture section and added a codebase
  snapshot to STATUS.md. The suite is now 900 tests (up from the 740 recorded
  earlier; the Postgres backend's tests skip without `CONTINUUM_TEST_POSTGRES_DSN`).

- **`docs/api/cli.md` now lists every CLI subcommand (#360).** The command
  table was missing 14 shipped subcommands (`start`, `status`, `complete`,
  `budget`, `tree`, `fork`, `compact`, `observe`, `gateway`, `briefing`,
  `gate`, `hooks`, `reconcile`, `dashboard`), so a newcomer had no reference
  for them even though README and the `--help` output list them. Each new row
  mirrors the `help=` string from `src/continuum/cli/main.py:build_parser`;
  the table now matches the parser exactly (33 rows, one per subcommand).

- **README CI and Codecov badges (#199).** The README badge row gains CI status and Codecov badges linking to the workflow runs, so the health of `main` is visible without opening Actions. Repo chrome, docs-only, counted as a gap conservatively.

- **README measured-count refresh and docs housekeeping (#273).** Corrects the README tool count, event-type count, test-suite size (about 1300 tests), lines-of-code recomputation and contributors list to match the live tree. Follow-up to the earlier ten-to-eleven correction, which already landed in the Changed section.

- **README restructure and install/related-work split (#274).** The README is tightened from 597 to 361 lines, moving dependency tables, extras matrix, Postgres setup and verification commands into `references/install.md`, `references/adapters.md` and `references/related-work.md`. The change also removes em dashes repository-wide. Docs-only.

- **STATUS full-gate audit and architecture docs (#299).** `STATUS.md` gains a dated full-gate audit section for `main` at 2026-08-24 (pytest, ruff, mypy, GHCR publish verification), the README measured counts are refreshed, and `docs/ARCHITECTURE_EVOLUTION.md` section 19 documents the #275 and #277 features otherwise unlogged. The citation-audit entry (#261) is unrelated and does not cover this.

- **CONTRIBUTING pre-commit example with ruff hooks (#337, #341).** `CONTRIBUTING.md` documents a pre-commit setup using `astral-sh/ruff-pre-commit` (ruff check and ruff-format hooks) so contributors can auto-check lint and format before committing. Contributor-facing docs-only.

### Fixed

- **`continuum_confirm` hid handler refusals (#371).** `confirm_gate` now
  invokes the handler inside `_refusal_reaches_the_caller`, so domain errors
  such as a missing run retain their useful `ToolError` message.

- **JSON booleans passed every integer check in the budget registry (#429).**
  `isinstance(True, int)` holds in Python, so `true` was accepted wherever
  `.continuum/budgets.json` requires a positive integer: as
  `default_max_attempts` or a per-type `max_attempts` it silently meant a cap of
  1, and an authorization-bound `counter` of `true` became 2 after one
  increment. A registry whose contract everywhere else is to fail loudly instead
  quietly meant something other than what was written. `load_budgets` now
  refuses booleans in all four positions with `BudgetConfigError`. This is a
  deliberate behaviour change rather than a coercion: a config that contains
  `true` in one of those positions loaded before and raises now, which is the
  point, because the value it was silently taking was not the one the operator
  wrote. Integer configs are unaffected. `evaluate_budget` also normalises a
  per-type limit through `int()` so a hand-built mapping cannot leak a bool into
  the `max_attempts` figure the `continuum budget` report renders.

- **`save_budgets` rewrote the registry in place, so a crash mid-write could
  truncate it (#427).** The write went through `Path.write_text`, which opens
  the target with mode `w` and truncates before writing, with no staging file and
  no `os.replace`. A crash, an OOM kill or power loss between truncation and
  flush left a zero-length or half-written `budgets.json`, and every later
  `load_budgets` then raised. Because the gate is fail-closed, that refused every
  budget-gated claim until an operator repaired the file by hand. The bytes now
  land in a sibling temporary file (same directory, so the rename stays within
  one filesystem), get flushed and fsynced, and are moved over the target with
  `os.replace`, which is atomic on POSIX and Windows alike. A save that dies
  leaves the previous registry intact and no litter behind. Losing the last
  increment on an abrupt exit is an acceptable price for a counter registry;
  losing the registry is not, and #413 turns this into a write per claim attempt.
  Two things the staging file must not change on its way in: `mkstemp` creates at
  0600 and `os.replace` carries those bits onto the target, so the staged file is
  chmod'ed to the existing registry's own mode first, or to `0o666 & ~umask` when
  there is no existing file - matching what `write_text` produced, because hooks,
  sidecars and CI steps read this registry under their own uid and a save that
  locks them out is worse than the truncation staging prevents. And the rename
  itself is a directory change, so the parent directory is fsynced after the
  replace; flushing only the staged file's contents left the new registry
  loseable by the very crash it guards against. Both steps are best effort: a
  filesystem without permission bits keeps the tighter 0600, and Windows (which
  cannot open a directory as a descriptor) is left exactly as durable as before.
  Swapping an inode for a rewritten one also changes two further things
  `write_text` did not, so both are re-established: the path is resolved before
  staging, because a `budgets.json` that is a symlink to a shared registry was
  written *through* by `write_text` and would be *replaced* by `os.replace`,
  leaving every other reader of the shared file - and `load_budgets`, which still
  reads through the link - on the counters from before; and the replaced inode's
  uid and gid are put back onto the staged file, because mode 0640 names a group
  without saying which one, and a fresh `mkstemp` inode belongs to whichever group
  the saving process sits in. Ownership is written before the mode, since `chown`
  clears setuid and setgid on some systems and a `chmod` running second would
  silently undo that. A refused `(uid, gid)` is retried as the gid alone, which is
  the half that grants access to anyone but the owner, and a `chown` that cannot be
  performed at all does not fail the save: as with the chmod and the flush, turning
  an unreproducible permission into a refused claim is the wrong direction for a
  fail-closed gate.

- **Budget rejections named the rule but not the offending value, and one still
  named the config by relative path (#326, #426).** "needs a positive integer
  `'max_attempts'`" was the same sentence for a missing field, a float, a string
  and a boolean, so a registry hand-converted from YAML with `3.0` where `3` was
  meant sent the operator back to re-read a line that looked correct. Every
  rejection now appends the value and its type (`got 3.0 (float)`), including the
  authorization-bound `counter` and `max_attempts` checks. Separately, the
  `action_types` failure interpolated the caller's `path` rather than the
  resolved one, the single straggler #351 and #333 left behind: an absolute input
  hid it, because the two spellings coincide there, but a hook, sidecar or CI
  step passing a cwd-relative path produced a message naming a file the reader
  could not open. It now resolves like every other message in the module.

- **One unprojectable event bricked every projecting command, with no route back (#383).**
  A log whose fold fails is intact as a chain but dead as a run: because the fold
  validates each intermediate state, no later event could correct an earlier bad
  one, so `status`, `resume`, `inspect`, `replay`, `show-contract`, `validate`,
  `briefing` and `compact` all raised on precisely the runs that needed them,
  while the action tools (which fold only ACTION_* events) kept authorising real
  side effects that recovery could not assess. The fold can now degrade instead
  of raising: `project` and `project_incremental` accept
  `on_unprojectable="raise"|"degrade"`, defaulting to `"raise"` so every existing
  caller sees byte-for-byte today's behaviour. Degrade mode stops at the earliest
  refused event and returns the last-good prefix marked
  `SemanticState.status = INVALID` with `unprojectable_at_sequence`,
  `unprojectable_event_type` and a condensed `unprojectable_reason`; it never
  skips past the break, and if nothing folds before the break it still raises,
  since a partial answer invented from nothing would be worse than an error. The
  recovery engine folds with degrade enabled, so a poisoned log yields a
  `request_human` verdict naming where folding stopped instead of a pydantic
  traceback, and CLI `status`, `inspect` and `replay` report the same break and
  exit non-zero. The break also reaches the machine-readable contract instead of
  living only in prose: a new `repair_log` repair step makes `required_actions`
  name real work, `next_allowed_action` points at it rather than falling through
  to a rendered "continue" over a `requires_human` verdict, `verified` entries
  are qualified with the last-good sequence, and `invalidated` records the
  projection itself. The diagnostic call sites opted in are the engine's restore
  path, the CLI status/inspect/replay surfaces, the serve sidecar's progress
  report and dependency dedup, and the benchmark's strategy readout; the MCP
  write-path guard `_project_candidate` and the checkpoint capture surfaces
  deliberately keep raising, because accepting or pinning a partial fold would
  launder it into authoritative state. Repair/amend and fork-from-last-good-prefix
  remain future work and are not attempted here.

- **`MODEL_CHANGED` had no writer, so `expected_model` could never validate (#370).**
  The event type was defined, treated as checkpoint-worthy by the trigger policy and
  projected into `SemanticState.model`, but nothing in `src/` ever emitted it. The
  validator's model component could therefore only ever answer "no model recorded
  for this run, cannot compare against ...", the `expected_model` parameter on
  `continuum_resume` and `continuum_validate` could never do anything, and
  `RepairKind.REVALIDATE_MODEL_STATE` with its "pass `--model <name>`" guidance was
  unreachable. A parameter that cannot be satisfied is worse than an absent one,
  because its presence implies the check is covered, and a different model resuming
  another model's work is exactly the drift the surrounding architecture exists to
  catch. `continuum_checkpoint` gains optional `model_id` and `provider`, emitting
  `MODEL_CHANGED` when the value actually changes, so drift now reports
  `requires_review` naming both models instead of `unknown`. Attached to
  checkpointing because it is the same kind of statement as `env`: here is what the
  world looked like when this state was saved. Recorded as `EXTERNAL_AGENT`, since
  an agent naming its own model is self-reporting, but the comparison against a
  later `expected_model` stays independent of that claim. `provider` carries forward
  when omitted, so naming the model alone cannot erase a provider recorded earlier,
  and omitting `model_id` records nothing rather than asserting absence.

- **The retry budget counted per action type, blocking work that never failed (#368).**
  `attempts_for_type` folded on `action_type` and ignored the idempotency key, so
  the limit capped a run's distinct unsettled work of a type rather than retries of
  one operation. Three different recipients each failing once, with zero retries
  anywhere, exhausted the default budget of three and refused a fourth that had
  never been attempted, so any fan-out with more failures than the limit deadlocked
  mid-run. The default applies with no config file present, so this was live in any
  project that had never configured budgets. New `attempts_by_key` counts per
  idempotency key, which is the operation's identity and is stable across retries
  because re-claiming after FAILED or COMPENSATED copies the existing action.
  `attempts_for_type` now reports the worst single operation, which is the figure
  the claim site compares against the limit, so `continuum budget` agrees with what
  is enforced; it is deliberately not the sum across keys, since that measures
  distinct work and nothing here caps that. The limit stays configured per type,
  because that is the unit an operator thinks in. The exhaustion message also fits
  the state it fires in: it names the specific operation, drops the advice to
  reconcile existing attempts (useless when every prior attempt is settled FAILED),
  and says the registry file may need creating rather than raising.

- **Same-named files in different directories collapsed into one action (#365).**
  With no explicit `key` the exact argument hash misses on two differently-spelled
  paths, so the identity fallback decides, and it compared basenames.
  `/tenants/acme/report.csv` and `/tenants/globex/report.csv` both reduced to
  `{report.csv, report}`, containment held both ways, and the second claim was
  answered `proceed=false` carrying acme's `external_id` and the guidance "Already
  performed. Reuse the previous result; do not repeat it." Globex was never
  notified, which is the silent swallow the fallback exists to prevent, and
  per-directory files with conventional names are a common fan-out shape. Leaf
  comparison was introduced so a re-rendered path (`invoices/INV-5.pdf` for
  `/data/invoices/INV-5.pdf`) would still deduplicate, so the container is now set
  aside rather than discarded: new `location_tokens` returns exactly what
  `leaf_tokens` drops, and `_identity_match` additionally requires the locations to
  agree. `same_location` compares by path suffix rather than equality, which is the
  shape drift actually takes, so the re-rendering case still matches while two
  fully-qualified paths agreeing on nothing but the filename do not. A side that
  names no path at all makes no claim about location and so contradicts nothing,
  which keeps the field-rename case working. Comparison stays purely lexical, with
  no filesystem or working-directory resolution, so the answer is identical on
  every machine.

- **`complete` could launder an `UNKNOWN` action into `COMPLETED` (#366).**
  `ActionLedger.complete` had no status guard, so a side effect whose real-world
  outcome nobody could determine, a charge that timed out after the request was
  sent, could be recorded as a clean success in one call: no evidence, no note,
  and an `ACTION_RECORDED` event indistinguishable from an ordinary first-time
  completion. `continuum_list_actions` then reported `unresolved: 0` and the
  recovery blocker was gone, with nothing in the log to show the decision had been
  made by assertion. The incentives pointed straight at it, because
  `continuum_complete_action` is the tool an agent is told to call routinely, sits
  on the same mutation allowlist as everything else, and accepts the key already in
  hand, while the evidence-gated route through `reconcile` was the harder one.
  `complete` now settles only a claim still in flight, plus a repeat report of one
  already `COMPLETED`, since a caller retrying after a dropped response asserts
  nothing new. `UNKNOWN`, `FAILED`, `COMPENSATED` and `REQUIRES_REVIEW` are refused
  with a message naming the status and pointing at `reconcile`, which takes the
  same decision but demands the caller stand behind it and records
  `ACTION_RECONCILED` with the note.

- **`continuum verify` certified a run whose log could not be projected (#382).**
  `verify` re-audits the hash chain, which is a statement about integrity, and an
  unprojectable log is perfectly intact: the offending event was written through
  the normal path and hashed like any other. Nothing in that audit evaluates
  whether the fold satisfies its own invariants, so the one health-shaped command
  an operator reaches for during an incident answered "13 events, no violations"
  for a run on which `resume`, `status`, `inspect`, `replay`, `validate` and
  `briefing` all failed. `verify` now reports both verdicts and exits non-zero
  when the fold fails, so `continuum verify "$RUN" && ./resume.sh` short-circuits.
  New `first_unprojectable_event` names the sequence, event type and the specific
  constraint that failed, folding one event at a time onto the previous state so
  the scan is a single linear pass rather than a re-projection per prefix.
  Archived events are folded alongside the live ones, because after compaction
  (#239) the live log starts at the anchor and no longer contains `RUN_STARTED`;
  reading only the tail would report every compacted run as broken. The
  projection is attempted only once the chain verifies, since folding a tampered
  log to say where it stops projecting would describe events that cannot be
  trusted to say anything. Repairing such a log is a separate gap, tracked in
  #383.

- **`self_report_guidance` said nothing was wrong over an unresolved action (#369).**
  The note exists to explain a `request_human` caused only by unverified
  self-reporting, and it is deliberately withheld when anything else is also
  blocking. Its predicate scanned `decision.validation.report.statuses`, but an
  uncertain action reaches `request_human` through `decision.uncertain_actions` and
  never appears in the report, so the check saw goal and progress alone and stayed
  true. The result was a single `continuum_resume` response whose contract read
  `recovery_status: requires_human` because a side effect's outcome was unknown,
  next to guidance reading "Nothing is wrong with this run" and "Work is not
  blocked", pointing the agent past the one thing the system exists to stop it
  walking past. The ledger is now part of the test. The dependency half of the
  predicate was already correct and is covered by a test that keeps it that way.

- **Recovery guidance named an identifier the settle tools rejected (#367).**
  `continuum_resume` reports uncertain actions by `action_id` in five places
  (`next_allowed_action`, the contract's `required_actions`, `human_steps`,
  `informed_retry.avoid` and the rendered report), and `human_steps` spelled out
  a `continuum_reconcile_action(action_key=<action_id>)` call. The ledger keyed
  only on the idempotency key, so following that instruction verbatim failed with
  `no action recorded for key ...`, and no MCP surface exposed the value that
  would have worked: `list_actions` and `uncertain_actions` reported `action_id`,
  the `UnknownSideEffect` response omitted the key entirely and left a
  12-character truncated prefix in free text as its only trace, `arguments_hash`
  from `continuum actions --json` looks like a key but is a different hash, and
  `continuum reconcile` needs a registered probe. An `UNKNOWN` action created
  over MCP was therefore unreconcilable through every documented interface.
  `ActionLedger.resolve_key` now accepts either space and `_require` returns the
  resolved key, so `complete`, `fail`, `reconcile`, `compensate` and
  `flag_for_review` all take an `action_id` or a key and settle under the fold's
  own key either way. `UnknownSideEffect` carries `action_key` and `action_id`,
  which `continuum_intercept_action` returns on the unknown path, and both
  `continuum_list_actions` rows and `continuum_resume`'s `uncertain_actions` gain
  `action_key`. The unmatched-identifier message names both spaces and says how
  to list them.

- **One `continuum_record_progress` call could permanently brick a run (#364).**
  The `completed + failed > total` guard only fired when `total` was passed in
  the same call. Omitting it skipped the guard while projection still folded the
  `total` recorded earlier, so the invariant was evaluated against a limit the
  call never mentioned. Worse, the handler appended before it projected, so the
  rejected event was already durable when validation failed, and because the
  fold validates each intermediate state no later event could correct it. Every
  projecting surface for that run then stayed dead permanently: `record_progress`,
  `checkpoint`, `validate` and `resume` over MCP, plus `status`, `inspect`,
  `replay`, `show-contract` and `briefing` over the CLI. The action tools kept
  working throughout, so the run could go on authorising real side effects while
  recovery was unable to say whether continuing was safe, and `continuum verify`
  still reported the chain as intact. The new `_project_candidate` helper folds
  the log with the candidate payload appended and commits only if that succeeds,
  so the write path now rejects exactly what the read path would reject rather
  than approximating it one field at a time. The cheap argument checks are kept
  ahead of it because they answer without touching storage and name the offending
  argument. The commit passes `expected_sequence`, because validation and append
  are two statements: a second writer landing `total=50` between the read and the
  write of a `completed=75` that omits `total` would otherwise compose a log
  neither payload would have been allowed to produce on its own. Losing that race
  re-validates against the new head and retries, bounded, since losing it says
  nothing about whether the update is valid.

- **`ActionLedger` could not serialise concurrent claims on one key (#345).**
  `claim()` deduplicates by folding the log and then appending, with nothing
  between the read and the write, so processes racing on one key could each be
  told to proceed: eight threads, eight go-aheads, eight charges. The outcome was
  decided purely by thread scheduling, and the event chain verified clean either
  way, so no integrity check could catch it. `docs/multi_agent_isolation.md`
  already specified the remedy ("one run, one owner at a time") and
  `RecoveryLedger` implemented it, but `ActionLedger` (the class whose entire
  purpose is at-most-once side effects) had no lease parameter. It now accepts an
  optional `LeaseCoordinator` and acquires the run's lease around `claim` and
  every settle method (`complete`, `fail`, `reconcile`, `compensate`,
  `flag_for_review`), so eight simultaneous claimants collapse to exactly one
  go-ahead and one `STARTED` slot; the losers raise the new `ClaimLockError`, or
  `UnknownSideEffect` where the winner's slot was already open, and neither
  performs the effect. The lease is reentrant for its own holder so an agent that
  correctly took the run lease first is not locked out of its own ledger, and
  `holder_id` is required rather than defaulted, because a shared default would
  make two processes look like one holder and silently defeat the protection.
  Omitting `lease` leaves the single-process path exactly as it was. Atomic
  claiming in storage, which would drop the caller's obligation entirely, remains
  open on #345.

- **Em dash in the CI lint job name.** The `mypy` step in `ci.yml` was named
  with an em dash, violating the no-em-dash house rule (#266); renamed with a
  comma. Found while editing the file for the wheel-artifact job.

- **Anchored verification trusted the first live row of a compacted run
  (PR #253 review, security).** After compaction, `verify_events` checked
  whether an EVENT_LOG_ANCHORED event existed anywhere in the live log and,
  if so, treated the first surviving row as a trusted genesis: its own
  `prev_hash` and sequence became the walk's starting point. Deleting the
  boundary events therefore left a "valid" chain, and verify returned success
  for a run whose anchor era had been erased. Both engines now read the
  newest `events_archive` row and require the live chain to continue it
  exactly (its hash as the expected `prev_hash`, its sequence plus one as
  the expected sequence), and SEQUENCE_GAP and BROKEN_CHAIN are enforced on
  anchored logs like any other. The archive itself is deep-audited in the
  same pass: every archived row is re-digested and chain-linked from sequence
  1, so editing or truncating history in `events_archive` fails verify
  instead of hiding behind a healthy live tail. Regression tests cover the
  tampered row, deleted boundary event, truncated archive and emptied archive
  cases through both `verify_events` and the CLI exit code.

- **`continuum compact` committed the anchor marker separately from the
  archive move (PR #253 review).** The marker append was one transaction and
  the INSERT/DELETE into `events_archive` another, so a crash between them
  left an anchored live log whose prefix never reached the archive, with
  verify then trusting a genesis that was never earned. Both engines now
  append EVENT_LOG_ANCHORED inside the same transaction as the archive move
  (SQLite through the shared IMMEDIATE transaction via a conn-taking
  `_append_chained` helper; Postgres through an explicit `transaction()`
  block around the three statements, since the connection runs in autocommit
  mode), and the docstrings document the real order: forced anchor checkpoint
  first, then the atomic marker-plus-move.

- **`replay` reported `verified: true` unconditionally for compacted runs
  (PR #253 review).** The anchored branch folded the restored checkpoint
  forward over the post-anchor tail but hardcoded the pass, so replay exited
  0 even when the folded state disagreed with the stored version; the
  corruption-detection contract was silently lost for every compacted run.
  The branch now re-folds only the stored version's own prefix and compares
  fingerprints exactly as the plain path's `_verify_against_stored` does,
  returning CORRUPTED with `verified: false` on mismatch. A dead
  `base is None` test (restore always returns a state) was removed along with
  the hardcoded flag.

- **Exactly-once reset at the compaction boundary (PR #253 review).** The
  action ledger folded only live events, so after compaction every archived
  ACTION_* claim vanished from the fold and a month-old completed side effect
  could be claimed fresh and fired again. `ActionLedger` now folds archived
  events too (`Storage.read_archived_events`, empty by default, implemented
  by both engines), `protected_call` takes its decision fold from the ledger
  rather than a raw event scan, and the old compaction test that exercised
  the guard with an unrelated key was replaced by one asserting an archived
  completed action is a cache hit that never re-runs the callback.

- Smaller PR #253 review items: EVENT_LOG_ANCHORED joined `_NON_PROJECTING`
  so anchored replays no longer count it under `ignored_types`; `compact`
  gates on a new `supports_compaction` capability flag instead of surfacing
  a raw NotImplementedError on engines without the archive table; and two
  copy-paste artifacts (a duplicated `MIGRATIONS` comment line, an incomplete
  clause in this changelog) were cleaned up.

- **The `continuum serve` sidecar exported a `MUTATING` constant describing an
  authentication policy it does not implement, and no test pinned the real one
  (issue #95, reported by @abyyxhek).** `MUTATING` names seven of the ten
  methods, and `_auth_check` gated the shared secret on membership in it, but
  `_auth_check` had no call sites anywhere in the tree, and `dispatch` calls
  `self.auth.verify` unconditionally, so `resume`, `validate` and `list_actions`
  do require a token despite being absent from the set. `SidecarAuth`'s docstring
  documented the same phantom rule ("every mutating call must present the
  matching `auth_token`"). The behaviour is the correct one and the description
  was wrong: unlike the MCP server, whose mutating-only policy is deliberate and
  pinned by `test_read_only_tools_stay_open_to_anyone`, the sidecar is reachable
  by any process that can speak to its pipe, and its reads are worth closing for
  what they return (`resume` hands back the goal string, `list_actions` the
  arguments and results of external side effects). Gating them costs nothing by
  default, since an unset `CONTINUUM_SERVE_TOKEN` disables authentication
  entirely. So the dead `_auth_check` is deleted, `MUTATING` keeps its export as
  descriptive write-vs-read metadata with a docstring stating that it does not
  govern authentication, and `SidecarAuth` and `dispatch` now document the real
  policy and why it diverges from the MCP server's. The gap was not only
  cosmetic: the existing auth tests all drove `record_progress`, so the suite was
  consistent with *both* policies, and reinstating the mutating-only gate in
  `dispatch` opened all three read-only methods to unauthenticated callers
  without turning a single existing test red. `tests/test_serve.py` gains three
  regression tests, twelve cases in all, every method in `list_methods()`
  refused without the secret, the read-only methods refused although `MUTATING`
  omits them, and a read-only method still succeeding with it, verified red
  against that reintroduced gate.

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
  `'C:\\Users\\ASUS\\no-such-dir\\agent.db'`, not the path the operator passed,
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
  whose dependency is absent, and `mcp/server.py` imported `MCPServer`,
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
    `external_dependencies`, so a moved dataset was rendered in
    `environment_changes` while the verdict stayed `safe: true` with the reason
    "all components verified against the current environment". The core validator
    was never wrong: given a declared dependency it already yields `CONFLICTED`
    and `safe_to_resume=False`. The gap was that no MCP client could declare one,
    and the existing test appended `DEPENDENCY_DECLARED` straight to storage.
    Checkpointing now records each pinned resource as a `DEPENDENCY_DECLARED`
    event, so the declaration is durable across projections and restores, covered
    by the hash chain, and carries `EXTERNAL_AGENT` provenance, which does not
    weaken the check, since a dependency's status comes from comparing two
    snapshots rather than from trusting the claim. Only new or re-pinned resources
    are appended, so checkpointing on a schedule does not grow the log. The
    `serve` sidecar shared the defect verbatim and is fixed identically; the two
    surfaces must not disagree about whether drift is safe.
  - *`continuum_list_actions` under-reported an interrupted action.* A claim left
    `STARTED` by a crash reported `side_effect_uncertain: false` while
    `continuum_resume` described the same action as an unknown outcome, the
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
    and only if that fails move the `-wal` aside, never unlink it, restoring it
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
  the validator has no way to verify recorded model-specific assumptions,
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


- **Regression test for the checkpoint environment round-trip.** `tests/test_storage.py`
  exercises the checkpoint/reload path end to end: a checkpoint is written with a
  declared dependency and a captured `EnvironmentSnapshot`, the `SQLiteStorage` handle is
  closed, a *fresh* `SQLiteStorage` is opened on the same file, and the environment is
  asserted to survive the round-trip. The reloaded run is then assessed against an
  unchanged environment and must resume as safe, proving `StateValidator.validate_dependency`
  sees the dependency as unchanged rather than treating a missing baseline as
  *added/breaking*. This path (serialising `StateCheckpoint.environment` through the
  checkpoint `body` column and restoring it) had no coverage; this is added test
  coverage for an untested path, not a fix for a defect.

- **RecoveryLedger gate, escalation and reconcile correctness (#176, #177, #178, #183).** Corrects three ledger findings: approval of one gate no longer clears later gates, escalation survives compaction, and the high-water drift mark is accurate, plus related cleanups. The Phase 5 ledger entry predated these fixes, so no Fixed entry existed until now.

- **Five audit fixes from the 2026-08-22 sweep (#201, #202, #203, #204, #205, #206).** `continuum_confirm` over MCP now requires its own `CONTINUUM_MCP_CONFIRM_TOKEN`, closing the self-certification exploit reopened by #35. `continuum checkpoint` for a missing run now exits 2 with `no such run` instead of a projection error. `continuum_record_progress` validates counters before writing `RUN_STARTED`, so a rejected call writes nothing. `continuum start --goal` gives the CLI a way to create a run at all, fixing the dead-end hint. `STATUS.md` known-issues table is refreshed. Each fix carries regression tests.

- **Windows portability for hooks, test suite and smoke script (#252).** `continuum hooks install` no longer writes silently dead commands on Windows (it previously joined via `shlex.quote`), and `shlex.split` parsing plus the smoke script are now portable. Existing Windows entries covered issues #81, #87 and #94, none matched this set, so no changelog entry existed.

- **Postgres storage ships LangGraph tables (#251).** `PostgresStorage._SCHEMA` now includes the schema-v4 `lg_checkpoints` and `lg_stores` tables, so the Postgres backend matches the SQLite schema. The contract-suite rewrite that proved the gap landed in the same PR and was already covered under the Production server mode entry, but the silent schema gap for Postgres deployments was not stated anywhere.

- **Retry budget no longer defeats idempotency, and diagnostics corrected (#309, #307, #308, #311).** The budget gate now runs after deduplication and is scoped per idempotency key, so an already-completed action returns `proceed: false` with the stored result even at budget, and a workflow with many distinct operations is no longer blocked at the default of three. The inverted diagnostic when `expected_model` is omitted and the `expected_model` silently inert case are also corrected. Each carries regression tests.

- **Security and usability fixes: attest-verify, fork child, dashboard 404 (#348).** `continuum attest-verify` now recomputes the head hash instead of trusting the stored value, so a tampered chain no longer reports SIGNED. `continuum fork` produces a child run that readers can open, and the dashboard no longer answers 200 for a missing run. All three were silent failures with no keyword hit for attest.

- **SQLiteStorage close is now idempotent (#320, #347).** Calling `close()` twice no longer raises or leaves the handle in an ambiguous state, so teardown in tests and server restart paths is safe. The behaviour fix was unlogged and had no #320 token until now.

- **Gate config errors name an absolute path, first half (#333, #340).** `load_gate_config` now resolves the config path to absolute before `GateConfigError` messages, so a relative invocation from a subdirectory shows which file was read. This is the first half of #333, covering two of the gate loader's path-bearing messages.

- **Every registry error names an absolute path, completing #333 (#351).** The remaining two gate-config messages plus the budgets, reconcilers and gateway registry loaders now include the resolved absolute path. Combined with #340, every registry load error meets the #333 bar.

- **Use-after-close now says the database is closed (#320, #347, #352).** The regression from #347 that turned use-after-close into a generic error is restored to `database is closed`, and idempotent close is pinned by regression tests. The fix was unlogged.

- **Authentication failures name token fields and stop echoing secrets (#318, #353).** MCP and serve auth errors now name the exact server and client token fields (`CONTINUUM_SERVE_TOKEN` and `auth_token` or `CONTINUUM_MCP_TOKEN`) and never echo the configured secret. Both auth paths carry regression tests asserting the hint is present.

### Added, Phase 12: CONTINUUM-Bench (minimal harness)

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

### Added, Phase 8: command-line interface

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
  validator treats as *unverified* rather than *unchanged*, not checking must not resemble
  checking and finding nothing wrong.
- `benchmark` exits `4` and states plainly that no numbers are published because none have been
  measured.
- 48 new tests (473 total), including real-subprocess invocation and a shell-pipeline test proving
  `&&` short-circuits on unsafe state.

### Fixed

- **`verify` and `actions` exited 0 for a run that does not exist.** An absent run has a trivially
  valid (empty) event chain and no recorded actions, so `continuum verify $TYPO && deploy` reported
  a clean bill of health for a name nobody had ever written to, precisely the failure the
  exit-code contract exists to prevent. All eight run-scoped commands now check existence first and
  report `NOT_FOUND` consistently. Found by driving the installed binary by hand; the test suite
  had not covered it.
- `replay` on a missing run reported "the log never recorded RUN_STARTED", diagnosing the wrong
  problem.
- `RunNotFound` and `CheckpointNotFound` inherit from `KeyError`, whose `__str__` applies `repr()`
  to the message, so users saw `error: "no such run: 'ghost'"`, quoted twice.
- CLI output was written to a block-buffered stdout while hints went to stderr, so when piped the
  hint could appear *before* the report it referred to. Output is now flushed at each emit.
- `render_diff` duplicated the field name for progress counters (`completed: completed: 1 → 50`).
- A weak test: `test_every_recovery_mode_maps_to_a_code` iterated only *known* modes, so it never
  reached the unmapped-mode fallback it claimed to protect. Mutation testing caught that defaulting
  the fallback to `OK` went undetected; the replacement exercises an unclassified mode directly.

### Added, Phase 7: recovery engine

- `RecoveryEngine` (`continuum.recovery.engine`) reducing three independent signals, validation
  statuses, action-ledger state and checkpoint integrity, to one `RecoveryMode`.
- **The most cautious applicable signal wins.** Each signal proposes a mode and the engine takes the
  maximum on an explicit severity ordering (`RESUME < REPAIR_AND_RESUME < REPLAN < WAIT <
  REQUEST_HUMAN < ROLLBACK < ABORT`). These signals genuinely co-occur, a run can have a stale
  dataset *and* an uncertain side effect, and returning whichever was noticed first would let the
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
  integrity hash, a contract editable between issue and enforcement would gate nothing.
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

### Added, Phase 6: action ledger and idempotency

- Idempotency keys (`continuum.actions.idempotency`) derived from action type plus canonically
  hashed arguments, so argument order never matters but a changed value always does. `scope`
  separates runs; `volatile` excludes fields like retry counters that would otherwise make every
  retry look like a new action. Nothing is excluded by default, collapsing two genuinely different
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
  external system, the only strategy producing evidence), `AssumeNotOccurredReconciler` (requires
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

### Added, Phase 5: state validation

- Environment capture (`continuum.environment.snapshot`): pluggable `EnvironmentProvider` with
  `StaticProvider`, `FileProvider` (content hashes, so touching a file does not invalidate work),
  `ValueProvider` and `CallableProvider`. Providers never raise, a resource that cannot be
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
  rather than raw evidence, which is legitimate provenance and occurs in every well-formed
  reasoning chain. Findings now count as citable support. False alarms are how real ones get
  ignored.

### Removed

- A dead branch in progress validation that re-checked a counter invariant the `Progress` model
  already enforces on construction and deserialization. Verified unreachable rather than left as
  untestable code; the invariant is tested at the model level.

### Added, Phase 4: checkpoint creation

- Checkpoint policies (`continuum.checkpoint.policy`): `ManualPolicy`, `IntervalPolicy`,
  `EventPolicy`, `SemanticPolicy`, `ContextPressurePolicy` and `HybridPolicy`, plus a
  `default_policy()` that checks explicit requests, side effects and meaning before falling back to
  time, so a checkpoint reports the real reason it was taken rather than "the timer went off".
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
 , goal, verified progress, stale state, items requiring review, valid decisions, pending work,
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

### Added, Phase 3: SQLite persistence

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
  reload, phantom corruption caused by storage, not by tampering.
- `sqlite://` URL parsing no longer strips the leading slash of an absolute path, which had caused
  the database to be created in the working directory instead of the requested location.

### Added, Phase 2: semantic state representation

- Deterministic projection (`continuum.state.semantic`): folds an event prefix into `SemanticState`.
  Guarantees reproducibility (no wall-clock dependence) and prefix-closure, so a run can be recovered
  up to the log's `trusted_through` boundary. Unknown event types are counted and reported rather
  than raising, keeping forward-written logs recoverable.
- `Provenance` and `Origin` on every state component: each item traces back to the event that
  produced it, and `reproducible` distinguishes re-derivable state from asserted or inferred state.
- Pluggable extraction (`continuum.state.extractor`): `StateExtractor` protocol,
  `DeterministicExtractor` (no model, no network), optional `LLMExtractor` that may only add
  components, never modify or delete recorded facts, tagging everything `Origin.LLM` and
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

### Added, Phase 1: data models and event system

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

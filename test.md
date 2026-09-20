# MCP surface audit  -  2026-08-19

An end-to-end test of CONTINUUM's MCP server against its own documented claims,
driven over the live stdio protocol rather than through mocks. Every tool result
was checked against the SQLite store directly, because a system whose thesis is
"agent-reported state is never self-certifying" should not be audited by trusting
what its own tools report.

- **Server under test:** `python -m continuum.mcp` over stdio JSON-RPC 2.0
- **Clients:** Claude Code (registered MCP server) and a purpose-built raw
  JSON-RPC client for the authorization and protocol cases
- **Independent verification:** `sqlite3` queries and `SQLiteStorage.verify_events`
- **Suite at audit time:** 803 passed, 4 skipped (README claimed 740  -  understated)
- **Suite after fixes:** 817 passed, 4 skipped

## Contents

[Method](#method) · [Results](#results) · [Findings](#findings) · [Fixes applied](#fixes-applied) · [Limitations confirmed as documented](#limitations-confirmed-as-documented) · [Reproducing](#reproducing)

## Method

Each claim was turned into a falsifiable check with a stated expected outcome,
then driven through the protocol. Where a claim passed, the *dangerous inverse*
was also tested  -  a deduplication test that only proves duplicates are suppressed
says nothing about whether real work is being silently skipped.

Two audit findings were only visible because of this. The action ledger was tested
in both directions (suppress a true duplicate; permit genuinely new work), and the
environment check was re-tested after clearing the self-certification gate, because
`safe: false` for the *wrong reason* looks identical to a passing check.

## Results

| # | Claim | Method | Result |
|---|---|---|---|
| 1 | Agent-reported progress is not self-certifying | `record_progress` → `resume` | **Pass.** `safe: false`, `request_human`, reason cites `external_agent` |
| 2 | Provenance is durably recorded | `sqlite3` on `events.source` | **Pass.** Rows persist as `source = external_agent` |
| 3 | Human confirmation unblocks resume | `confirm` → `resume` | **Pass.** Flips to `resume` / `safe: true` |
| 4 | Duplicate side effects are refused | `intercept` → `complete` → re-`intercept` | **Pass.** `proceed: false` with `previous_result` |
| 5 | Dedup survives argument drift | Field rename + absolute→relative path | **Pass.** Resolves to the same `action_key` |
| 6 | Distinct work is not suppressed | Two charges differing only in amount | **Fail → fixed.** See [F1](#f1) |
| 7 | Crash mid-action is surfaced | Claim, never complete | **Pass.** `resume` blocks on `reconcile_action` |
| 8 | `list_actions` flags unresolved rows | Inspect the interrupted row | **Fail → fixed.** See [F3](#f3) |
| 9 | Reconciliation frees a retry | `reconcile(occurred=false)` | **Pass.** `safe: true`, action re-claimable |
| 10 | Tamper-evident event log | Rewrote a payload in SQLite | **Pass.** `TAMPERED_CONTENT` + cascading `BROKEN_CHAIN` |
| 11 | Environment drift invalidates state | Checkpoint pinned, then hash changed | **Fail → fixed.** See [F2](#f2) |
| 12 | Mutating tools deny by default | Unconfigured server, raw client | **Pass.** Denied; "No callers are currently permitted" |
| 13 | Read-only tools stay ungated | Non-allowlisted caller calls `validate` | **Pass.** Allowed |
| 14 | Shared secret stops impersonation | `CONTINUUM_MCP_TOKEN` set, wrong/absent token | **Pass.** Fail-closed |
| 15 | WAL sidecar auto-recovery | Read `_open_server_storage` + measured blast radius | **Latent defect → fixed.** See [F4](#f4) |

### Tamper-evidence detail

Rewriting a completed charge's amount directly in SQLite, leaving the `hash`
column untouched:

```
ok              : False
trusted_through : 10        (was 16)
VIOLATION: TAMPERED_CONTENT seq=11  stored hash does not match recomputed digest
VIOLATION: BROKEN_CHAIN     seq=12  prev_hash does not match predecessor digest
```

Detection is exact and the trust boundary rolls back to the last good event.

### Post-fix verification

Re-run of the full sequence over stdio against the fixed code:

```
1  self-cert gate      : before confirm safe=False/request_human -> after safe=True/resume
2  $5000 after $100    : proceed=True   (want True)
3  true retry of $100  : proceed=False  (want False)
4  drift retry (email) : proceed=False  (want False)
5  crash mid-action    : unresolved=1 row.outcome_unresolved=True
6  resume blocked      : safe=False next=reconcile_action:action_5bbcf08f...
7  after reconcile     : safe=True mode=resume
8  env unchanged       : safe=True   (want True)
9  dataset moved       : safe=False reason=external_dependency q3_dataset is conflicted
                         deps=[('q3_dataset','conflicted'), ('schema','valid')]
10 resume w/ drift     : safe=False mode=repair_and_resume next=revalidate_dependency:q3_dataset
                         progress preserved={'completed':12,'failed':1,'pending':87,'total':100}
```

Both directions hold at once: drift blocks resume, a clean environment still
resumes, only the moved resource is invalidated, and completed work is preserved
through the repair.

## Findings

### <a id="f1"></a>F1  -  Distinct actions deduplicated against each other

**Severity:** high (silently skips a real side effect)
**Status:** fixed during the audit, by the maintainer, while testing was in progress

A `$5000` charge was suppressed against an earlier `$100` charge to the same
recipient. Live over MCP:

```json
{ "proceed": false, "previous_result": {"charged": 100},
  "guidance": "Already performed. Reuse the previous result; do not repeat it." }
```

Two defects combined in the keyless drift fallback:

1. `identity_tokens` collected only `str` values, so numeric arguments  -  the very
   thing distinguishing the two charges  -  never contributed to identity.
2. `_identity_match` treated a **single** overlapping token as proof of sameness
   (`if not (incoming & known)`), so a shared recipient was enough.

This is the failure mode `idempotency.py`'s own module docstring calls worse than
duplication, because it is quiet. The existing negative tests only covered
*fully disjoint* token sets (`INV-003` vs `INV-004`), so the case that overlaps in
one argument and differs in another was unguarded.

**Fix (maintainer):** intersection replaced by containment (`incoming <= known or
known <= incoming`), with `leaf_tokens` comparing path identity at the basename so
genuine re-renderings still match. Integers now tokenize.

**Audit contribution:** the monetary case is now pinned by two regression tests
(`test_a_larger_charge_is_not_deduplicated_against_a_smaller_one`,
`test_a_differing_amount_counts_whether_it_is_a_number_or_a_string`)  -  the sharpest
form of the rule, where one token is shared and the distinguishing value is numeric.

### <a id="f2"></a>F2  -  Environment drift was detected but did not block resume

**Severity:** high (reports state as verified when it is not  -  explicitly in scope per `SECURITY.md`)
**Status:** fixed in this change

With a checkpoint pinning `q3_batch_dataset` and the hash then changed:

```json
{ "safe": true, "mode": "resume", "invalidated": [],
  "environment_changes": ["q3_batch_dataset: sha256:aaaa1111 -> sha256:bbbb9999"],
  "reason": "all components verified against the current environment" }
```

The change was rendered and then ignored; the reason string was false.

Root cause was a gap between two correct halves. `state/validator.py`
short-circuits when a state declares no dependencies:

```python
if not state.external_dependencies:
    return state
```

and `continuum_checkpoint` passed `env` only as an `EnvironmentSnapshot`, never
declaring the resources. So the diff had nothing to invalidate and staleness had
no edges to propagate along. The core library was never broken  -  given a declared
dependency, the identical change yields `CONFLICTED` / `safe_to_resume=False`. The
MCP `env` parameter was decorative for safety purposes while its own tool
description promised "a dependency that moved since the checkpoint invalidates the
findings built on it."

The existing test for this (`test_validate_flags_a_changed_dependency`) declared
the dependency by appending `DEPENDENCY_DECLARED` straight to storage  -  something
no MCP client can do  -  which is how the gap survived a suite that covered the
machinery.

### <a id="f3"></a>F3  -  `list_actions` under-reported the interrupted row

**Severity:** low (misleading recovery diagnostics)
**Status:** fixed in this change

An action left `STARTED` by a crash reported `side_effect_uncertain: false` while
`continuum_resume` described that same action as `outcome unknown`. The
`unresolved` count was correct, so the aggregate was right while the row a human
would read said the opposite. `side_effect_uncertain` is only set on *escalation*
to `UNKNOWN`, which has not happened yet for a freshly interrupted claim.

### <a id="f4"></a>F4  -  WAL "self-healing" could destroy committed transactions

**Severity:** high blast radius, low reachability (a latent durability bug, not observed live)
**Status:** fixed in this change

`_open_server_storage` deleted both sidecars on a startup `OperationalError`,
justified by a docstring claiming they are "reconstructable from the main
database." That is true of `-shm` and false of `-wal`: the write-ahead log holds
transactions that are committed but not yet checkpointed.

Measured on the audit database  -  main file **4 KB**, WAL **420 KB** holding all
16 events. Deleting the WAL without checkpointing first:

```
WITH -wal present    : 16 events, ok = True
after sidecar removal:  0 events, ok = True     <- rows in events table: 0
```

The loss is total *and* silent: an emptied database still verifies as an intact
chain, so the failure presents as success. Only reachable when the initial open
raises `OperationalError` (SQLite normally replays a hot WAL by itself), which is
why this is latent rather than observed  -  but it is exactly the hard-kill scenario
the README advertises as a feature.

## Fixes applied

| Finding | Change | Files |
|---|---|---|
| F2 | `checkpoint` now declares each pinned `env` resource as a `DEPENDENCY_DECLARED` event, so drift has something to invalidate and propagate through | `src/continuum/mcp/server.py`, `src/continuum/serve/server.py` |
| F3 | Each `list_actions` row carries `outcome_unresolved`, derived from ledger state so it cannot disagree with `resume` | `src/continuum/mcp/server.py`, `src/continuum/serve/server.py` |
| F4 | Staged, least-destructive recovery: drop the reconstructable `-shm` first; only if that fails move `-wal` aside (never unlink), restoring it if the retry still fails | `src/continuum/mcp/server.py` |
| F1 | Regression tests pinning the monetary false-positive case | `tests/test_action_ledger.py` |

Notes on the F2 fix:

- Declared as **events**, not written onto the checkpoint's state object, so the
  declaration is durable across projections and restores, is covered by the hash
  chain, and carries `EXTERNAL_AGENT` provenance automatically.
- Provenance does not weaken the check. Unlike goal and progress, a dependency's
  status comes from comparing two snapshots rather than from trusting the claim,
  so the *comparison* stays independent of the agent that named the resource.
  Confirmed: `REQUIRES_REVIEW` degradation applies only to goal and progress.
- Only new or re-pinned resources are appended, so checkpointing on a schedule
  with an unchanged environment does not add an event per resource per call.
- The `serve` sidecar was fixed identically. It shared F2 and F3 verbatim, and the
  two surfaces must not disagree about whether drift is safe. It does not share F4
  (no WAL recovery path).

New tests: 14 added (5 MCP environment/dependency, 1 MCP `list_actions`, 2 ledger
regression, 3 serve, 4 WAL recovery  -  including one asserting the log is never
unlinked and one asserting a second crash cannot overwrite the first's evidence).

## Limitations confirmed as documented

These are real constraints, correctly disclosed, and are **not** counted as findings.

**The hash chain is tamper-evident, not tamper-proof.** An attacker with write
access to the database can rewrite a payload and recompute every downstream hash;
the re-chained log then verifies clean:

```
AFTER RE-CHAINING: ok = True, violations = [], trusted_through = 16
  charge amount now reads: {"amount_usd": 999999, ...}
```

`references/attestation.md` states plainly that the chain answers "was this chain
altered by accident or by a buggy writer?" and not "was this chain signed by an
authority I trust?", with signing scoped as design-only. `SECURITY.md` also puts
direct filesystem write access out of scope. Accurate as documented  -  though the
README's headline "hash-chained tamper-evident event log" reads stronger than the
sub-reference that qualifies it.

**`clientInfo.name` is asserted, not verified.** Any local process declaring
`"claude-code"` receives full mutating access. `SECURITY.md` lists this out of
scope and names `CONTINUUM_MCP_TOKEN` as the mitigation; the mitigation was tested
and is fail-closed (no token and wrong token both refused, correct token allowed).
`authz.py`'s own docstring is candid: "enough to separate cooperating agents, and
not enough to stop a hostile one on its own."

## Reproducing

```bash
# Full suite, lint, types
python -m pytest -q                      # 817 passed, 4 skipped
python -m ruff check src/ tests/         # All checks passed
python -m mypy src/continuum/mcp/server.py src/continuum/serve/server.py

# The specific behaviours this audit added coverage for
python -m pytest tests/test_action_ledger.py -k "charge or amount" -v
python -m pytest tests/test_mcp_server.py -k "env or dependency or unresolved" -v
python -m pytest tests/test_mcp_server.py -k "wal or shm or quarantin or log" -v
python -m pytest tests/test_serve.py -k "env or unresolved" -v
```

To drive the live server the way this audit did, register it as an MCP server and
exercise `record_progress → checkpoint → intercept_action → complete_action →
resume`, then verify independently against the store rather than trusting the
tool output:

```bash
sqlite3 "$CONTINUUM_DB" "select sequence, type, source, hash from events order by sequence;"
python -c "from continuum import SQLiteStorage; print(SQLiteStorage('$CONTINUUM_DB').verify_events('<run_id>'))"
```

Note that a WAL-mode database keeps recent commits in `<db>-wal`; copy the
sidecars alongside the main file or the events will appear to be missing.

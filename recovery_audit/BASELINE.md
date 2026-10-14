# Phase 1 - Baseline Repository State

**Captured:** 2026-10-11
**Auditor method:** all measurement taken against a frozen isolated worktree, because the shared
main checkout is being edited concurrently (evidence below).

## Repository identity

| Field | Value |
|---|---|
| Repository root | `/Users/adhinans/Desktop/Open source/Continuum` |
| Branch | `main` |
| HEAD commit | `c6e00411e246bc76baf93d4c1692237c9173e489` |
| HEAD subject | `fix(ci): remove blocking trustabl scanner workflow (#1396)` |
| `origin` (push/fetch) | `https://github.com/Adhi1-2/CONTINUUM.git` (fork) |
| `upstream` (fetch) | `https://github.com/Cyrax321/CONTINUUM.git` |
| `origin/main` | `c6e00411` (== local `main`) |
| `upstream/main` | `c23385cd4de3faea80c5a53618d1bf8093d4c5a5` |
| Divergence | local `main` is **91 commits behind** `upstream/main`, 1 commit ahead |

The mission names `Cyrax321/CONTINUUM` as the project repository. The checked-out fork HEAD is
91 commits behind that upstream. **All findings in this audit are against the checked-out tree,
not against upstream `main`**, unless explicitly stated. Several issues found here are already
fixed upstream; those are flagged rather than reported as current defects of the upstream project.

## Working tree status at audit start

```
 M src/continuum/actions/ledger.py
 M src/continuum/cli/main.py
 M src/continuum/plugins/registry.py
 M src/continuum/recovery/contract.py
 M src/continuum/recovery/engine.py
 M src/continuum/recovery/ledger.py
 M src/continuum/state/validator.py
 M src/continuum/storage/base.py
 M src/continuum/storage/postgres.py
 M src/continuum/storage/sqlite.py
 M tests/test_family.py
?? recovery_audit/          <- this audit's artifacts (added by me)
```

These 11 modified files are **pre-existing uncommitted work by the repository owner**, not mine.
They implement issue #744 (dependency-scoped attempt budgets in the recovery ledger), plus a
`resolve_prior` helper and a `report` CLI subcommand. **I did not create them and I did not modify
them.** They are part of the audited state because they are what is actually checked out.

### The uncommitted work is mid-refactor

The WIP renames the recovery-ledger entry field `dependency` → `scope` but is incomplete:

- `RecoveryLedgerEntry.scope` exists (`src/continuum/recovery/ledger.py:221`), and
  `RecoveryLedger.record_attempt` was rewritten to use it, **but** the code paths that still say
  `e.dependency is None` were changed to `e.scope is None` in one spot while the tests and several
  call sites still read `e.dependency`. Result:
  `AttributeError: 'RecoveryLedgerEntry' object has no attribute 'dependency'` in
  `tests/test_recovery_ledger.py::test_record_attempt_tags_the_dependency`.

This is the cause of the `test_recovery_ledger.py` / `test_recovery_engine.py` failure cluster.
It is pre-existing breakage in the uncommitted work, not something I introduced, and per the rules
I have **not** repaired it.

## The committed HEAD is not importable

This is the single most important baseline fact. Two defects exist in the *committed* tree at
`c6e00411`, independent of any uncommitted work:

### Defect B0-1 - `SyntaxError` in `sqlite.py` (commit `c6e00411`)

```
$ git show c6e00411:src/continuum/storage/sqlite.py   # around line 543
    def compact_run(self, run_id, *, through_sequence=None, environment=None):
        """Archive the pre-anchor prefix of a run's log (issue #239).
        ...
        rejected (issue #705) instead of silently deleting the anchor and
        every live row, which would leave the next append minting a fresh
        genesis and fork the hash chain away from the archive.
                                                                              <- no closing """
        lv = self.latest_version(run_id)
```

The `compact_run` docstring is never terminated. The string literal swallows the entire method
body and terminates at the *next* triple-quote in the file, which belongs to `foreign_action`
at line 645. Python reports the damage as:

```
File "src/continuum/storage/sqlite.py", line 647
    O(log n) via the primary key instead of folding every run's events.
                                                             ^
SyntaxError: unterminated string literal (detected at line 647)
```

The reported line (647) is **not** the defect site (559) - the error surfaced two methods later.
Anyone reading the error at face value would look in the wrong place. Verified against a clean
worktree at `c6e00411` with no uncommitted changes applied.

**Already fixed upstream.** `upstream/main`'s `sqlite.py` has the closing `"""` plus a local
`from continuum.checkpoint.manager import CheckpointManager` import at that exact spot. So this
is a defect of the fork's lagging HEAD, not of the upstream project.

### Defect B0-2 - missing `_liveness_digest_value` (commit `c6e00411`)

`src/continuum/recovery/contract.py:239` (in the commit) calls `_liveness_digest_value`, which is
**not defined anywhere in the committed file**. The uncommitted WIP adds it at line 215. So at the
committed HEAD:

```
ImportError: cannot import name '_liveness_digest_value' from 'continuum.recovery.contract'
```

Combined with B0-1, **`import continuum` fails at the committed HEAD of this checkout.** The
uncommitted WIP is the only reason the working tree imports at all.

## Baseline test results

### Environment

- Python 3.13.5 (`/Users/adhinans/Desktop/Open source/Continuum/.venv`)
- pytest with `pyproject.toml` config: `testpaths=["tests"]`, `pythonpath=["src"]`,
  `addopts="-q --strict-markers"`, `asyncio_mode="auto"`
- Editable install `.pth` points at the main checkout's `src`, so a bare `pytest` in another
  worktree still imports the *main checkout's* copy. Every measurement in this audit sets
  `PYTHONPATH=<worktree>/src` explicitly to defeat that. This was verified by printing
  `continuum.__file__` from inside a test collection.

### Why baseline was re-taken in an isolated worktree

My first full-suite run (in the shared main checkout) produced **95 failures**. Before recording
it I noticed `tests/test_trajectory_report.py::test_cli_report_without_a_kind_is_a_usage_error`
failed there but **passed** in a worktree at the same commit with the same WIP. Cause: the shared
main checkout was being edited by a concurrent session while the suite ran. Direct evidence -
between 12:05 and 12:20 the uncommitted diff grew from **744 to 950 lines**, adding
`Storage._anchor_environment`, `read_all_events`/`read_archived_events` overrides, and edits to
`src/continuum/benchmark/phase6/scenarios.py`. A baseline taken on a moving tree is not
reproducible, so it was discarded.

**The authoritative baseline below is a frozen snapshot.** Worktree `/private/tmp/audit-recovery`,
detached HEAD at `c6e00411`, with the uncommitted WIP applied verbatim as a patch captured at
2026-10-11 12:05. That worktree is the reference tree for the rest of this audit.

### Authoritative baseline command and result

```bash
cd /private/tmp/audit-recovery
PYTHONPATH=/private/tmp/audit-recovery/src .venv/bin/python -m pytest \
    -q -p no:cacheprovider --junitxml=/tmp/audit_baseline.xml
```

Exit status: **1**

```
tests=3359  failures=80  errors=0  skipped=36  time=109.136s
```

Raw output: `recovery_audit/evidence/baseline_full.txt`
JUnit XML: `recovery_audit/evidence/baseline_full.xml` (copied from `/tmp/audit_baseline.xml`)

Note: this sandbox renders pytest with `COLUMNS=0`, so the human-readable `N passed, M failed`
summary line is absent from the tail of the output. Counts above are parsed from the junitxml.

### Baseline failure classification (all 80)

By exception type:

| Count | Exception | Character |
|---|---|---|
| 49 | `AssertionError` | mostly docs/README self-consistency + WIP behaviour |
| 14 | (no `E ` line - subprocess/CLI exit-code failures) | CLI/doctor/provider-config |
| 6 | `TypeError` | WIP signature changes |
| 4 | `AttributeError` | the `dependency`→`scope` rename |
| ... | `KeyError`, `OperationalError` | remainder |

By category:

1. **Documentation self-consistency drift (≈34 failures).** `test_docs_counts.py`,
   `test_docs_glossary.py`, `test_cli_docs.py`, `test_docs_event_counts.py`,
   `test_architecture_data*.py`, `test_precommit_config.py`, `test_projection_*.py`,
   `test_module_all_exports.py`. These assert that hard-coded counts in README/docs match the
   tree. Representative:
   ```
   AssertionError: /private/tmp/audit-recovery/README.md states inconsistent figures: [2864, 2906]
   ```
   README.md itself disagrees with README.md. Pre-existing, unrelated to recovery safety.

2. **WIP-induced (≈22 failures).** `test_recovery_ledger.py` (10), `test_recovery_engine.py` (2),
   `test_compaction.py` (3), `test_payload_offload.py` (2), `test_replayguard.py` (2), and parts
   of `test_cli.py` / `test_gateway.py`. All trace to the half-finished `dependency`→`scope`
   rename or to WIP behaviour that the concurrent session is still writing. Pre-existing in the
   sense that they exist independent of me.

3. **CLI surface drift (≈8 failures).** `test_cli.py::test_cli_command_count*`,
   `test_cli_docs.py`, `test_mcp_doctor.py`, `test_provider_config.py`. The WIP adds a `report`
   subcommand; the command-count expectations in docs/tests lag it.

4. **Unrelated environment (1).** `test_storage.py::test_postgres_url_is_routed_to_postgres_backend`
   - needs the optional `psycopg` extra, which is not installed here.

### Pre-existing failures I must not re-investigate

These 80 are the baseline. Any fix I make must not reduce them by weakening assertions, and any
test I add must not perturb them. Where a later phase reports a *new* failure, it is compared
against this list.

## Relevant source paths (recovery safety surface)

| Path | Role |
|---|---|
| `src/continuum/actions/ledger.py` | action ledger: `claim` / `complete` / `fail` / `reconcile` / `compensate` |
| `src/continuum/actions/idempotency.py` | idempotency keys, identity tokens, argument hashing |
| `src/continuum/actions/reconciliation.py` | reconciler registry |
| `src/continuum/recovery/engine.py` | `RecoveryEngine.assess` - the resume verdict |
| `src/continuum/recovery/contract.py` | sealed recovery contract |
| `src/continuum/recovery/ledger.py` | recovery attempt ledger (WIP-modified) |
| `src/continuum/recovery/gate.py` | gate enforcement |
| `src/continuum/recovery/planner.py` | repair plan |
| `src/continuum/recovery/restore.py` | checkpoint restore |
| `src/continuum/storage/base.py` | storage contract and its stated guarantees |
| `src/continuum/storage/sqlite.py` | SQLite backend, hash-chained append-only event log |
| `src/continuum/storage/postgres.py` | optional Postgres backend |
| `src/continuum/concurrency/lease.py` | run lease coordinators (in-process + SQLite) |
| `src/continuum/checkpoint/manager.py` | checkpoint creation and projection |
| `src/continuum/state/validator.py` | state validation |
| `src/continuum/models.py` | `Action`, `ActionStatus`, `UnknownSideEffect` |

## Existing recovery / idempotency / crash tests already present

The repository already has substantial recovery test coverage, consulted before writing any new
test (see `recovery_audit/ARCHITECTURE.md` and Phase 3 for what was reused vs added):

`tests/test_action_ledger.py`, `tests/test_action_ledger_concurrency.py`,
`tests/test_action_ledger_grants.py`, `tests/test_action_idempotency.py`,
`tests/test_action_index.py`, `tests/test_recovery_engine.py`, `tests/test_recovery_ledger.py`,
`tests/test_recovery_contract.py`, `tests/test_recovery_gate.py`, `tests/test_replayguard.py`,
`tests/test_reconcile_plugins.py`, `tests/test_compaction.py`, `tests/test_checkpoint*.py`,
`tests/test_storage.py`, `tests/test_storage_integrity.py`, `tests/test_validator.py`,
`tests/test_concurrency.py`, `tests/test_unknown_side_effect.py`,
`tests/test_semantic_projection.py`, `tests/test_family.py`, `tests/test_reconcile.py`.

## Phase 1 conclusion

The audited tree is **broken at baseline in three independent ways**:

1. The committed HEAD does not import (B0-1 syntax error, B0-2 missing symbol).
2. The uncommitted WIP that masks (1) is itself a half-finished refactor that fails ~22 tests.
3. 91 commits of upstream drift, so ~34 docs-consistency tests are stale.

Per the rules, I repaired **only what was necessary to conduct the audit**: the two import
defects were repaired *inside an isolated worktree only*, by applying the uncommitted WIP
verbatim, so the audit measures the tree the user actually has. **No file in the shared main
checkout has been modified by me.** The repo's own `recovery_audit/` directory is the only thing
I have added there, and it holds documentation and evidence only.

**No speculative recovery fixes have been started.**

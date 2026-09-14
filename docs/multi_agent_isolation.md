# Multi Agent Shared State Isolation

## Problem

Two agents acting on the same `run_id` can interleave checkpoint writes and
ledger appends. Without isolation, one agent's recovery anchor can be
overwritten or its ledger decision can be interleaved with another agent's
attempt count, corrupting the high watermark.

## Current mechanism

- `RecoveryLedger` can be constructed with a `LeaseCoordinator` (`src/continuum/recovery/ledger.py:209`). `append_decision` and `record_attempt` acquire the lease for the run (`_locked` in `src/continuum/recovery/ledger.py:222`) and raise `LedgerLockError` if the lease is held. The same coordinator is used by `continuum serve` sidecars.
- `ActionLedger` takes the same optional `LeaseCoordinator` (issue #345). Every mutating method (`claim` and each settle method) acquires the run's lease before folding the log and releases it after appending, raising `ClaimLockError` when the lease is held elsewhere. Unlike `RecoveryLedger` it is reentrant for its own holder, so an agent that acquired the run lease first is not locked out of its own ledger, and a `holder_id` is mandatory rather than defaulted, because a shared default holder would make that reentrancy indistinguishable from two agents colliding.
- `CheckpointManager` writes checkpoints and versions via `Storage` which is
  transactional in SQLite and Postgres. `prune` and `last_recovery_anchor`
  operate per `run_id` and version.

These cover single writer at the API boundary, but they do not define the
ownership model for the state itself.

## Ownership model

- **One run, one owner at a time.** An agent must hold the lease for the run
  before it writes events, checkpoints, or ledger entries. The lease `holder_id`
  should be a stable agent identity, not a transient process id, so audits can
  attribute mutations.

- **Lease scope.** The lease protects the tuple `(run_id, kind)` where `kind`
  is `state` or `ledger`. Today the ledger and the state share the same
  `run_id` lease. A future refinement is to split them so an agent can append
  ledger decisions while another agent streams progress events, but that
  requires a merge strategy for `project`, which is out of scope.

- **Lease TTL and renewal.** Use a short TTL with renewal while the agent
  holds the run. If the agent dies without releasing, the TTL expires and
  another agent can acquire. The current `LeaseCoordinator` interface already
  carries `ttl` (`src/continuum/concurrency/lease.py:1`).

## Checkpoint isolation

- Checkpoints are per `run_id` and versioned. Two agents checkpointing the
  same run concurrently will create two versions; the later version wins for
  `latest_checkpoint`. To avoid surprise, an agent that holds the lease should
  checkpoint only state it projected itself after acquiring the lease, not
  stale state from before the lease.

- Recovery anchors (`trigger == RECOVERY`) must be written while holding the
  lease and immediately followed by a ledger `append_decision` with the same
  `checkpoint_version` and `anchor=True`. The pair is the auditable proof that
  the anchor was intentional.

## Ledger isolation

- `record_attempt` already writes an anchored `human_required` gate once when
  `attempts >= max_attempts` (`src/continuum/recovery/ledger.py:306`). This
  marker must survive `compact` with `keep_anchors=True`, which the current
  implementation does. Concurrent `record_attempt` calls are serialized by the
  lease; without the lease the count can race. The spec requires the lease for
  any run where concurrent recovery is possible.

## Tenant binding

- The gateway enforces a bound tenant for memory-store routes (`src/continuum/gateway.py:227`). Only rendered keys starting with `mem:` are checked (`is_memory_key` in `src/continuum/gate.py:133`).

- Key shape is `mem:{store_id}:{tenant}:{record_key}`, for example `mem:pgvector_main:acme:rec-42`. The tenant is the third colon-separated segment. A key template such as `mem:{store_id}:{tenant}:{record_key}` renders the request body fields into this shape (`render_key` in `src/continuum/gateway.py:155`).

- The binding is configured in the gateway config file (default `.continuum/gateway.json`) as `bound_tenant` or `tenant`, read by `load_gateway_tenant` (`src/continuum/gateway.py:132`). A missing file means no binding. When set, the CLI threads it through `match_route` and `GatewayServer` (`src/continuum/cli/main.py`, `src/continuum/gateway.py:308`).

- Denial behavior is fail-closed and runs before the ledger check. A bound tenant `acme` with a request for `globex` is denied with `tenant mismatch: bound 'acme' but key ... carries tenant 'globex'`. A malformed `mem:` key with fewer than four segments is denied as `malformed memory key`. This is pinned by `test_gateway_tenant_deny` and `test_gateway_tenant_deny_before_ledger` in `tests/test_memory_forensic.py:142`.

## What to build next

- Add an integration test that spawns two `RecoveryLedger` instances with a
  shared `InMemoryLeaseCoordinator` and asserts the second `append_decision`
  raises `LedgerLockError` while the first holds the lease. This already
  exists in unit form but should be exercised through the checkpoint plus
  ledger pair.

- Document the lease acquisition order in `docs/CONTRIBUTING_ONBOARDING.md`
  and in the walkthrough. If a run is observed without a lease, `reconcile`
  should report `drift` and the run should not be resumed until a lease
  holder validates.

- Wire a coordinator through the `ActionLedger` construction sites that can face
  a second writer (`continuum serve`, the MCP server, the gateway). The class
  accepts one now, but those callers still build it unleased, so the operator
  gets the capability only by constructing the ledger directly. Each needs a
  decision about where its stable `holder_id` comes from.

- Make the claim atomic in storage, so the guarantee holds without cooperation
  from callers. A unique constraint over open slots per `(run_id,
  idempotency_key)` is the shape, but the ledger is event-sourced and a legitimate
  re-claim after `COMPENSATED` or `FAILED` writes a second record under the same
  key, so "open" has to be expressed against the fold rather than the table. This
  is the only option that removes the caller's obligation entirely.

This spec may spawn implementation issues for a `LeaseAwareCheckpointManager`
wrapper and for per kind lease splitting.

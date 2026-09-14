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

Memory-store keys are tenant-scoped by shape: a route whose `key_template`
renders to `mem:{store_id}:{tenant}:{record_key}` carries the tenant as the
third segment of the identity. That tenant segment is part of the key's
identity model, so it is enforced, not advisory.

- **Where the binding is configured.** The gateway reads an optional
  `bound_tenant` field (the alias `tenant` is accepted) from the same registry
  file as its routes - `.continuum/gateway.json` by default, or the `--config`
  path - via `load_gateway_tenant`
  (`src/continuum/gateway.py:132`). No field, or a blank one, means no tenant
  is bound and no tenant check runs.

- **What key shape it applies to.** Only rendered memory keys
  (`is_memory_key`, the `mem:` prefix, `src/continuum/gate.py:133`). A bound
  tenant never inspects non-memory keys; the check applies after
  `render_key` substitutes the request body into the template and before any
  ledger lookup.

- **Denial behavior.** When a tenant is bound and the rendered key's tenant
  segment differs, the request is denied at the gateway with
  `tenant mismatch: bound '<bound>' but key '<key>' carries tenant '<key's>'`
  - before the ledger-claim check, so an unclaimed cross-tenant call fails
  with the tenant reason, not an unclaimed one
  (`src/continuum/gateway.py:227`). A `mem:` key too short to carry a tenant
  segment is denied as malformed rather than guessed at. Without a bound
  tenant, no check runs and a cross-tenant call fails only through the
  ordinary claim rules (an unclaimed key is denied as `has no ledger claim`).
  Both orders are pinned in `tests/test_memory_forensic.py`
  (issue #566, parent #304).

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

# Memory mutation governance

Governing long-term memory writes as claimed, provenance-stamped, tenant-scoped side effects. CONTINUUM is not a memory system, the ledger and gate are the control plane.

## Why this exists

Long-running agents accumulate state in external stores such as pgvector, Mem0, Letta, Zep, or bespoke vector tables. Four failure classes live there:

* **Poisoned records** that persist across sessions and steer future behavior.
* **Memory drift** where a shortcut learned from a few atypical interactions is applied ever after.
* **Tenancy breaches** where retrieval crosses a user boundary. This is not a wrong answer, it is a data breach.
* **Erasure gaps** where nobody can enumerate what the agent wrote, so right-to-erasure cannot be honored.

Memory-store mutations are invisible to durability unless hand-registered in `.continuum/gate.json`. This guide fixes that by giving every memory write a stable identity that the ledger can deduplicate, scope to a tenant, and enumerate for deletion.

## Key convention

Every memory-mutating tool is registered in `.continuum/gate.json` with a stable key template that names its identity, not its arguments:

```
mem:{store_id}:{tenant}:{record_key}
```

* `store_id` identifies the concrete store, for example `pgvector_main`, `mem0`, `letta`.
* `tenant` carries the tenancy boundary, the user or workspace that owns the record. Different tenants never dedupe against each other because the tenant is part of the key.
* `record_key` is the record identity as the store defines it, for example a document id, chunk id, or memory id. Two writes with the same triple are the same record.

Templates are rendered from top-level tool arguments using `gate.render_key`. Values are stripped of surrounding whitespace (`gate.normalize_key_value`) so ` " rec-42 "` and `"rec-42"` are one key. The gate and the gateway share this rule, therefore a value written through one seam is recognized at the other.

### Tenant isolation

Same `store_id` and `record_key` under different `tenant` values produce different keys, so tenancy is enforced by identity itself:

* `mem:pgvector_main:acme:rec-42` does not collide with `mem:pgvector_main:globex:rec-42`.
* A cross-tenant retry is not a dedup, it is a distinct write under its own tenant namespace.

### Cross-run dedup

Memory is global to the store, not to the run that wrote it. The ledger key for `mem:` identities is therefore unscoped, so `action_index` can catch a double-write from a later run:

* Run `run_1` claims and completes `mem:pgvector_main:acme:rec-42`. The index stores it under its global key.
* Run `run_2` attempts the same triple. Even though run_2's local log is empty, a foreign lookup via `storage.foreign_action(global_key, exclude_run=run_2)` finds the earlier completion and the gate reports already completed. The effect is not sent twice.

This is the same exactly-once mechanism that protects invoices or charges, applied to memory records.

## Registration

Add entries under `tools` in `.continuum/gate.json`. Each entry names the tool as the harness sees it, the template, and the `action_type` the ledger will record.

Minimal example:

```json
{
  "tools": {
    "mem_write": {
      "key_template": "mem:{store_id}:{tenant}:{record_key}"
    }
  }
}
```

Worked pgvector and Mem0 examples live in `.continuum/gate.json.example`. Copy that file to `.continuum/gate.json` and adjust `store_id` and tool names to match your harness.

## Claim flow

1. Derive the rendered key from arguments, for example `store_id=pgvector_main, tenant=acme, record_key=doc-42` renders `mem:pgvector_main:acme:doc-42`.
2. Claim before firing:

```python
from continuum.actions.ledger import ActionLedger
from continuum.actions.idempotency import idempotency_key

rendered = "mem:pgvector_main:acme:doc-42"
key = idempotency_key("mem_write", None, scope=None, key=rendered)
outcome = ActionLedger(store, run_id).claim("mem_write", {}, key=rendered, scoped_to_run=False)
if not outcome.fresh:
    # already completed elsewhere, use outcome.result
    pass
```

3. Perform the store mutation, then complete or fail the claim. The gate denies any unclaimed call with instructions to claim first, and denies an already completed key with an already completed message. Uncertain outcomes are blocked until reconciled, exactly as for any other external effect.

For HTTP stores, the enforcing gateway derives the same key from the request body. Configure an upstream in `.continuum/gateway.json` with the same `key_template` and `action_type`:

```json
{
  "upstreams": [
    {
      "host": "pgvector.internal",
      "methods": ["POST"],
      "prefix": "/upsert",
      "action_type": "mem_write",
      "key_template": "mem:{store_id}:{tenant}:{record_key}"
    }
  ]
}
```

The gateway and the pre-tool gate share `normalize_key_value`, so one identity rule governs both seams.

## pgvector walkthrough

Tool: `pgvector.upsert` with arguments `store_id`, `tenant`, `record_key`, `embedding`, `metadata`.

Gate entry:

```json
{
  "tools": {
    "pgvector.upsert": {
      "key_template": "mem:{store_id}:{tenant}:{record_key}",
      "action_type": "mem_write"
    }
  }
}
```

Call 1, run_1, `pgvector_main, acme, doc-42`: renders `mem:pgvector_main:acme:doc-42`, claimed with `scoped_to_run=False`, completed after the upsert succeeds. Run_2 attempting the same triple sees the index entry and is refused as duplicate.

Call 2, run_2, `pgvector_main, globex, doc-42`: renders `mem:pgvector_main:globex:doc-42`, a different key, so it proceeds independently. Tenancy breach by key confusion is not possible because the tenant is inside the identity.

## Mem0 walkthrough

Tool: `mem0.write` with arguments `store_id`, `tenant`, `record_key`, `content`.

Gate entry:

```json
{
  "tools": {
    "mem0.write": {
      "key_template": "mem:{store_id}:{tenant}:{record_key}",
      "action_type": "mem_write"
    }
  }
}
```

Same rules apply. A Mem0 record written as `mem:mem0:acme:user-pref-7` is exactly-once across restarts and across runs that share the store. Deleting or updating uses the same triple, so the delete is also claimed and audited.

## Tenancy enforcement

When a run is bound to a tenant identity, only keys whose `tenant` field matches that identity should be claimed. The gate keeps the check simple: the tenant is literally inside the key, so a claim for `globex` cannot be mistaken for one for `acme`. If your harness enforces a run-level tenant, reject at the application layer when `tenant` in the arguments differs from the bound identity, before claiming. The ledger then never sees a cross-tenant write to confuse enumeration later.

## Poisoning forensics and erasure

Every memory write is a ledger row keyed by tenant namespace. Enumeration is therefore a filter:

```bash
continuum actions <run> --json | python -c "import json,sys; data=json.load(sys.stdin); print([a for a in data['actions'] if 'mem:' in a['action_id']])"
```

`continuum forget --tenant X` builds on this enumeration to list exactly what to delete externally and to append a tombstone event:

```bash
# Dry run: list what would be tombstoned
continuum forget --tenant acme --dry-run --json | python -m json.tool

# Tombstone and keep audit trail
continuum forget --tenant acme --reason "gdpr request" --json
continuum verify <run_id>  # still passes, chain keeps hashes
```

Forensic join links a poisoned record back to its origin. Each claim may carry `origin_digest`, the hash of the originating observation. Given a bad record, `ActionLedger.forensic_lookup(record_key)` and `forensic_join_across_runs(storage, record_key)` return the actions and their joined `PERCEPTION_OBSERVED` events, so sibling writes from the same contaminated origin can be enumerated:

```python
from continuum.actions.ledger import forensic_join_across_runs
hits = forensic_join_across_runs(store, "rec-42")
for h in hits:
    print(h["rendered_key"], h["origin_digest"], h["observation_event"])
```

Chain verification keeps hashes, so logical deletion does not break `verify()`. Physical removal of historical hashes is out of scope by design, the chain keeps evidence that something was written and later tombstoned. Tombstones are `MEMORY_TOMBSTONED` events, hash-chained like any other, with `hashes_kept: true` in the payload.

## Limitations

* Stores reachable through direct credentials in the sandbox that never pass through a gated tool or the gateway remain ungoverned. This is the same caveat that applies to shell commands that bypass the ledger. Governance covers what is claimed.
* The key convention governs identity, not truth. It does not judge whether a memory is correct, only who wrote which record on whose behalf and how to take it back.
* Vector recall quality, embedding policy, and compaction are out of scope. Those belong to the memory system itself.

## Verification

```bash
pytest tests/test_memory_gate_keys.py -q
ruff check src/ tests/ examples/
ruff format --check src/ tests/ examples/
mypy src/continuum --ignore-missing-imports
# also verify no em dashes in changed files
rg -n "$(printf '\\u2014')" src/ tests/ docs/ examples/ benchmarks/
```

See `.continuum/gate.json.example` for copy-pasteable pgvector and Mem0 templates and `src/continuum/gate.py` for the derivation and cross-run dedup logic.

## Benchmarks

Harness `benchmarks/memory_governance.py` measures the control plane cost:

* Key derivation: ~4 us per `derive_memory_key` on a laptop.
* Forensic join across 10 runs x 20 records (200 hits): ~37 ms.
* Gateway tenant deny (allow + deny): ~13 us per `match_route`.

These are cheap, deterministic operations. Numbers come from a real run of the harness, not estimates, so the durability cost is auditable. See `benchmarks/memory_governance.py` for the scripts that produced them.

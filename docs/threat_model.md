# Threat Model

This document scopes what CONTINUUM protects against, what it detects, and what it explicitly does not. It keeps trust claims honest.

## What CONTINUUM protects

- **Tamper of history**  
  The event log is hash chained and checkpoints are sealed. The recovery ledger is hash chained from `GENESIS` via `stable_hash`. `verify` and `verify_contract` detect any edit of a prior entry by hash mismatch. See `src/continuum/recovery/contract.py:42` and `src/continuum/recovery/ledger.py:377`.

- **Replay of stale state**  
  Replaying an old checkpoint that is behind a sealed contract is detected as drift by `RecoveryLedger.reconcile`, which compares live `state.version` to the ledger high watermark `max(contract.checkpoint_version)`. See `src/continuum/recovery/ledger.py:426`. Tested in `tests/test_ledger_replay.py:1`.

- **Forgery of a contract**  
  Every `RecoveryContract` is sealed with `integrity_hash = stable_hash(payload)` via `seal_contract`. `verify_contract` recomputes the hash and rejects a forged digest, reason, or evidence. Tested in `tests/test_contract_forgery.py:1`. No API seals without hashing.

- **Spoofed evidence**  
  Contract evidence is derived from the validator report via `_validation_evidence` in `src/continuum/recovery/contract.py:131`. Supplying external evidence does not inject into the sealed terms unless the caller explicitly overrides the field, and an empty evidence contract still seals but carries less evidence. Tested in `tests/test_evidence_injection.py:1`.

- **Duplicate side effects**  
  `ActionLedger` with stable idempotency keys and drift recognition prevents duplicate external effects. See `src/continuum/actions/ledger.py:1` and `tests/test_adapter_result_fuzz.py:1`.

- **Authority resurrection**
  A one-time credential or approval token consumed before a checkpoint must not become valid again after restore. `AUTHORITY_CONSUMED` events are hash-chained with `Origin.DETERMINISTIC` and never deduplicate, so every consumption is an auditable row. The gate checks the consumed set before forwarding and refuses reuse with `Authority <id> consumed at seq <n> by run <r>`. See `src/continuum/actions/authority.py:1` and `src/continuum/events.py:121` and `tests/test_authority_consumed.py:1`.

- **Silent constraint drops**  
  `CONSTRAINT_PINNED` hash-only pins verified across compaction and briefing via `account_pins_in_context` (#391, #418). A summary that omits a pinned constraint is flagged as `absent` past grace, `unverifiable` when truncated, and escalates to `REQUIRES_REVIEW` in strict mode. See `docs/guides/constraint-pinning.md` and `docs/concepts/constraint-pinning.md` and `src/continuum/state/semantic.py:account_pins_in_context`.

- **Stale causal ancestors via provenance graph (N-hop)**  
  Evidence invalidation propagates N hops through the ``caused_by`` DAG (``evidence -> finding -> decision -> action``). When a dataset changes, every downstream finding, decision and action reachable via ``caused_by`` is marked ``STALE`` (or ``CONFLICTED`` on cycles) with reason ``via caused_by from <parent> (N-hop staleness)``. The propagation walks the transitive closure, not just direct parents, and surfaces in ``RecoveryContract.invalidated``. The graph is built from ``read_all_events`` so compaction does not launder history. See ``src/continuum/state/validator.py:_propagate_caused_by`` and ``src/continuum/provenance/graph.py``. Tested in ``tests/test_provenance_staleness.py`` (issue #553).

## What CONTINUUM does NOT protect against

- **Full disk access by a remote attacker**  
  If an attacker can read and rewrite the database files and the ledger files, they can rewrite history and re seal the chain from genesis. The ledger is tamper evident, not tamper proof against an owner of the storage. Protect the files with filesystem permissions and, for remote storage, with authenticated storage access. Signer keys for attestation must be held outside the data directory.

- **Compromised large language model**  
  CONTINUUM judges recovery validity, it does not judge model honesty. A model that fabricates evidence strings before they reach the event log is outside the recovery boundary. Provenance records who asserted a fact so the distinction is auditable, but it cannot prove correctness of model content.

- **Unbounded resource exhaustion by adversarial probes**  
  Resource limits via `run_with_limits` in `src/continuum/recovery/limits.py:1` are opt in. Without a timeout a probe that hangs can block recovery. Callers that run untrusted probes should pass an explicit timeout.

- **Malicious local process impersonating another agent**  
  `MCP` authorization is by declared `clientInfo`, not authenticated identity. See `docs/CONTINUUM_MASTER_PLAN.md:6` for the explicit limitation. It keeps honestly named agents apart, it does not stop a deliberately impersonating local process.

- **Forged constraint presence markers**  
  A summarizer that forges `[pin:id:hash8]` without honoring the constraint will appear `present`. Pinning detects silent drops, not adversarial forgeries. Out of scope for v1, detectable by external detector in SNAGLINE companion repo #90. See `docs/guides/constraint-pinning.md` honest scope list.

## Assumptions

1. The storage backend is trusted to return what was written, except for the explicit tamper cases above.
2. The hash function `stable_hash` is collision resistant for practical purposes. It is not a full cryptographic audit log with external transparency.
3. Signer keys for attestation are generated and stored securely. Rotation is covered in `docs/CONTINUUM_MASTER_PLAN.md` and is not yet implemented.
4. Clocks are roughly correct for timestamp fields; they are not used for security ordering.

## Out of scope items

- Network level confidentiality and transport security.
- Differential privacy of agent data.
- Formal verification of invariants. The test suite provides property and adversarial tests but not proof.

## References

- `docs/CONTINUUM_MASTER_PLAN.md` section 6 for limitations that are explicitly not claimed.
- `docs/recovery_walkthrough.md` for an end to end trace of how the guarantees interact.
- Security adversarial tests: `tests/test_contract_forgery.py`, `tests/test_ledger_replay.py`, `tests/test_evidence_injection.py`.

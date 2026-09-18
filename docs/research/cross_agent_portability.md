# Cross Agent Recovery Portability

Can a contract sealed by one agent framework be verified by another. This note specs the minimal portable subset of `RecoveryContract` for interop.

## Core idea

`RecoveryContract` in `src/continuum/recovery/contract.py:1` is already framework agnostic. It is built from validation output and a repair plan, both of which are derived from the event log, not from framework internals. Any agent that can read the same event log and environment snapshot can verify the same contract, regardless of whether it uses LangGraph, LangChain, or the generic adapter.

## Portable subset

### Required fields

These must be present and byte identical for verification to succeed:

- `run_id` (string)
- `checkpoint_version` (int)
- `recovery_status` (`RecoverySafety` enum)
- `next_allowed_action` (string or null)
- `integrity_hash` (string, hex digest of the payload)

A verifier recomputes `stable_hash` over the payload excluding `integrity_hash` and `created_at`, as done in `verify_contract`. If `seal_key_id` is present, the HMAC path in `docs/signing_key_rotation.md` is tried first, then the hash paths.

The digest rules are published, versioned and covered by a checked-in corpus: see `docs/contract_compatibility.md` (#764). That document, not this note, is the spec an external verifier implements against.

### Optional fields

These may be omitted or empty and verification still succeeds via the legacy payload fallback. They are useful for humans and for richer UIs but not required for the safety decision:

- `verified` (list of strings)
- `invalidated` (list of strings)
- `required_actions` (list of strings)
- `evidence` (list of strings, Phase 1)
- `reason` (string, Phase 1)
- `created_at` (datetime, excluded from the hash)
- `seal_key_id` (string or null, rotation spec)

A contract with empty optional fields still seals and verifies, but carries less evidence. See `tests/test_evidence_injection.py:1` for the empty evidence case.

## Compatibility rule

1. Old verifier reading a new contract: fails closed with a diagnostic naming the unknown contract version, and does not guess what the newer digest covers. This replaces the earlier "ignore unknown optional fields" guidance, which could not distinguish a field the verifier does not understand from one the producer removed.

2. New verifier reading an old contract: succeeds by trying the contract's declared version and then each older one, as tested in the Phase 1 backward compatibility tests and pinned by the `legacy` corpus fixtures.

## Out of scope

- Framework specific `model` or `tool` names inside `required_actions`. These are strings, so they travel, but their interpretation is framework specific.
- Ledger transport. Portability of the ledger file (`FileLedgerBackend` JSONL) is separate from contract portability.

No external claims are made. The subset is derived from the current implementation and its existing tests.

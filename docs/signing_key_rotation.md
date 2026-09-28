# Signing Key Rotation Spec

This spec extends the Phase 1 contract seal with an optional keyed seal that can be rotated without breaking verification of older hash only contracts.

## Current seal (Phase 1)

`seal_contract` in `src/continuum/recovery/contract.py:66` computes `integrity_hash = stable_hash(payload)` where payload excludes `integrity_hash` and `created_at`. `verify_contract` recomputes the same hash and also checks a legacy payload without `evidence` and `reason` so contracts sealed before those fields existed still verify.

## Goal

Allow a deployment to seal with a keyed HMAC and to rotate keys, while old hash only contracts and contracts sealed with a prior key still verify.

## Design

- **Contract fields**  
  Add optional `seal_key_id: str | None` and keep `integrity_hash`. When `seal_key_id` is `None` the contract is hash only as today. When it carries a key id the hash is `HMAC-SHA256(payload, key)` keyed by that id, still stored in `integrity_hash`.

- **Sealing**  
  `seal_contract(contract, *, key_id: str | None = None, key: bytes | None = None)` When no key is given behavior is unchanged. When a key is given the payload is the same deterministic JSON as today and the digest is `hmac.new(key, payload_bytes, sha256).hexdigest()`, stored with `seal_key_id`.

- **Verification**  
  `verify_contract(contract, *, keys: Mapping[str, bytes] | None = None)` tries in order:
  1. If `seal_key_id` is set and `keys` contains it, recompute HMAC with that key and compare.
  2. Fall back to the existing stable hash checks (current payload and legacy payload).
  3. Return False otherwise.

  This preserves backward compatibility: an old contract with `seal_key_id == None` verifies via path 2.

- **Rotation**  
  Operators publish a mapping `key_id -> key_bytes`. New contracts use the newest `key_id`. Verification accepts any id in the map, so a grace window where two ids are valid covers in flight contracts. Revoking a compromised id is removing it from the map, which causes contracts sealed with it to fail verification until re sealed with a valid id.

- **Migration**  
  No migration of existing contracts is required. They remain hash only and continue to verify. A background job may optionally re seal selected ledger decisions with the new key by reading the ledger entry payload, recomputing with the new key, and appending a fresh decision.

## Non goals

- External transparency log or certificate transparency.
- Automatic key distribution. The spec assumes the operator distributes the key map to verifiers.
- Changing the ledger entry hash scheme. Ledger entries remain `stable_hash(entry.content())` as in `src/continuum/recovery/ledger.py:262`.

## Alternatives considered

- Store HMAC in a separate field. Rejected because it would require verifiers to know which field to check. Reusing `integrity_hash` with `seal_key_id` as discriminator keeps one verification entry point.
- Per ledger key rather than per contract key. Rejected because contracts are the unit verified independently of ledger transport.

## Open items

- Key storage for the `FileLedgerBackend` directory. Recommended to keep keys outside the ledger directory with filesystem permissions 0600.
- Attestation signer key rotation in `src/continuum/security/attestation.py` should follow the same `key_id` pattern when it moves from Ed25519 file keys to HMAC.

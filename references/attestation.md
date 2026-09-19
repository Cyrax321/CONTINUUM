# Event-chain attestation design

Status: design only. The signing/verification primitives exist and are tested
(`src/continuum/security/attestation.py`, `tests/test_attestation.py`), but the
`continuum attest` / `continuum verify --sign` CLI surface is not built yet,
pending review of this design.

## Why

CONTINUUM's event log is already tamper-evident: `storage/sqlite.py` verifies
`prev_hash` against the head and `event.hash` against `event.digest()` on every
append, raising `CorruptedRecord` on mismatch. That answers "was this chain
altered by accident or by a buggy writer?"

It does not answer "was this chain signed by an authority I trust?" A downstream
system (a compliance reviewer, another agent, a CI gate) may want proof that a
run's history had not been altered *as of a signature by a known key*, without
re-running the whole log. That is exactly the trust question Dapr 1.18's
"Verifiable Execution" markets, and CONTINUUM already has the hash chain it
needs; attestation is a thin optional layer over it.

## What gets signed

The signer attests a specific, verifiable point in a run's history:

```json
{
  "run_id": "run_4821",
  "trusted_through_seq": 17,
  "chain_hash": "<head event hash>",
  "signer": "ci-bot",
  "timestamp": "2026-08-17T...Z",
  "public_key": "<PEM>",
  "algorithm": "ed25519+sha256",
  "signature": "<base64 Ed25519 over canonical JSON of the above minus signature>"
}
```

- `chain_hash` is the head event's `hash` for `run_id` (the log's root).
- `trusted_through_seq` is the sequence number that hash covers, taken from the
  run's `trusted_through` record already maintained by `events.py`.
- The signature covers every field except `signature`, canonicalized with the
  existing `to_json` (sorted keys), so it is byte-stable.

## Commands (proposed)

```
continuum attest <run_id> --key signer.pem [--signer ci-bot] [--out attest.json]
    # resolves the run's head hash + trusted_through_seq from storage, signs,
    # writes the attestation document.

continuum verify <run_id> --attest attest.json [--expect-hash <head>]
    # 1. verifies the Ed25519 signature against the embedded public key
    # 2. recomputes the run's live head hash and compares to chain_hash
    # 3. reports SIGNED / ALTERED / UNTRUSTED
```

`--sign` on `verify` is not needed; verification never requires the private key.

### Environment variables

Both signing inputs can be supplied by flag or by environment variable; a flag
on the command line wins when both are present.

`CONTINUUM_SIGNER_KEY`
: The private key PEM path `continuum attest` signs with when `--key` is not
passed. Without either, the command refuses with "no signing key" rather than
looking for a default.

`CONTINUUM_SIGNER`
: The signer name embedded in the attestation document when `--signer` is not
passed. It records who issued the signature; nothing derives authority from it,
so a verifier that cares about identity must still check `public_key`.

## Threat model and limits (documented honestly)

- Attestation proves *authenticity of a claim about a point in history*. It does
  not by itself prove the chain is currently trusted; the verifier must compare
  `chain_hash` to the live head and must know the expected `public_key`.
- It is optional. The recovery path never imports it; `cryptography` is pulled in
  only by the `[attest]` extra and imported lazily inside the functions.
- It is not a substitute for the existing integrity checks; it is additive.
- Key management (where the signer key lives, rotation, revocation) is out of
  scope for v1 and must be documented as the operator's responsibility.

## Open questions for review

1. Should `attest` default `signer` from an env var (e.g. `CONTINUUM_SIGNER`)?
2. Should attestations be stored alongside the run (in the event store) or as
   standalone files only?
3. Is `trusted_through_seq` enough, or should we also attest a content hash of
   the reconstructed semantic state, not just the event log root?

These should be answered before the CLI surface is implemented.

# Event-chain attestation design

Status: implemented. `continuum attest` / `continuum attest-verify` and the
signing primitives behind them (`src/continuum/security/attestation.py`,
`tests/test_attestation.py`, the attest cases in `tests/test_cli.py`) are built.
The propagation-token layer described in "Propagation tokens" below is built too
(`src/continuum/security/lineage.py`, `tests/test_lineage.py`); the workload-
identity binding in #745 and the real-time `resume` enforcement hook remain open.

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

## Commands

```
continuum attest <run_id> --key signer.pem [--signer ci-bot] [--out attest.json]
    # resolves the run's head hash + trusted_through_seq from storage, signs,
    # writes the attestation document.

continuum attest-verify <run_id> --attest attest.json
    # 1. verifies the Ed25519 signature against the embedded public key
    # 2. recomputes the run's live head hash and compares to chain_hash
    # 3. reports SIGNED / ALTERED / UNTRUSTED
```

`--sign` on `attest-verify` is not needed; verification never requires the
private key.

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

## Propagation tokens: delegating work across a boundary

An attestation answers "was this chain intact as of this signature?". It does
not answer the question a downstream service actually has when it receives
delegated work: "who handed me this run, over which checkpoint, for what purpose,
and is that handoff still within its validity?" That is what a lineage token
answers (`src/continuum/security/lineage.py`, issue #760), and it is the layer
`references/integration-architecture.md` Section 5 calls a propagation token,
matching Dapr's Workflow Attestation.

A token binds, under one issuer signature:

```json
{
  "version": "lineage-v1",
  "algorithm": "ed25519+sha256",
  "run_id": "run_4821",
  "checkpoint_version": 3,
  "trusted_through_seq": 17,
  "chain_hash": "<head event hash>",
  "contract_integrity_hash": "<stable_hash of the sealed contract's terms>",
  "attestation": { "...the signed attestation document above..." },
  "issuer": "ci-bot",
  "audience": "reviewer-svc",
  "purpose": "delegated code review",
  "issued_at": "2026-09-18T12:00:00+00:00",
  "expires_at": "2026-09-18T13:00:00+00:00",
  "public_key": "<issuer PEM>",
  "signature": "<base64 Ed25519 over canonical JSON of every field but signature>"
}
```

The contract is bound *by hash*, not by value: the token records the seal of the
terms the source run reached, so a downstream system that holds the contract can
confirm the token was issued over exactly those terms, while a token stays small
enough to hand across a wire. The verdict of the contract is deliberately not
carried; a token over a run that needs repair is still honest evidence of
lineage, and reading the verdict is the contract reader's job.

```
continuum lineage-issue <run_id> --key issuer.pem --purpose "delegated code review"
    [--audience reviewer-svc] [--ttl 3600 | --expires-at ...] [--out token.json]
    [--attest existing.json]
    # re-derives the sealed recovery contract read-only, signs the live head into
    # an attestation (or binds a pre-signed one), and issues the token.

continuum lineage-verify [run_id] --token token.json [--audience reviewer-svc]
    [--issuer-key pub.pem | --trusted-keys keyring.pem]
    # reports VALID / MALFORMED / UNSUPPORTED_VERSION / TAMPERED / EXPIRED /
    # WRONG_AUDIENCE / UNKNOWN_ISSUER / BROKEN_REFERENCE, and exits 0 only on
    # VALID.
```

`run_id` is optional on `lineage-verify`: with it, the sealed contract is
re-derived and its seal compared to the token's; without it, the token is checked
on its own contents, which is what a verifier with no access to the source store
needs. Either way verification writes nothing.

### Trust boundary

- A token is **evidence of origin and delegation, not a capability**. Holding a
  valid token does not authorize appending to the source run, resuming it, or
  claiming its contract's permissions. A holder who needs to act still needs the
  gate, the action ledger, and a run that is actually theirs. Downstream
  verification never implies authorization to mutate the source run; this is the
  boundary the issue refused to blur, and it is why the token is not a bearer
  credential.
- Fields are bounded by construction: no private keys, no raw environment
  payloads, no event history. The chain is one hash and one sequence number; the
  contract is one integrity hash. A token is safe to hand to an untrusted
  downstream system precisely because there is nothing in it to exploit.
- Expiry is always finite (default one hour). A delegation that never expires is
  a standing credential, which this token explicitly is not.
- The audience, when set at issuance, is *required* at verification: a token
  minted for service A does not verify against service B simply because B
  declined to pass `--audience`.
- The issuer key is a digest of the public key (`sha256:<hex>`, normalized so PEM
  whitespace does not matter), so a verifier's trusted-issuer set is a set of
  these. An unknown issuer fails closed.
- Key custody, rotation and revocation are the operator's responsibility, as with
  `attest`; the token has no revocation mechanism of its own, which is why the
  default TTL is short.
- The format is provider-neutral. Key-based attestations work today; the
  workload-identity binding in #745 can later mint the same envelope by
  supplying its own key, with no format change. An attestation may be signed by
  a different key than the token's issuer, since attesting a chain and
  delegating work are separate acts.

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

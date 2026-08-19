# Security

CONTINUUM's core is dependency-free, but two optional security features live
behind extras: cryptographic attestation of the event chain, and authentication of
MCP callers.

## Attestation

`continuum.security.attestation` signs a run's event chain so a third party can
verify it was not altered. Requires `continuum-agent[attest]` (the `cryptography`
package).

```python
from continuum.security.attestation import (
    generate_keypair, sign_chain, verify_attestation, Attestation,
)

priv_pem, pub_pem = generate_keypair()
attest = sign_chain(priv_pem, run_id, trusted_through_seq, chain_hash, signer="ci")
verify_attestation(attest)                              # True
verify_attestation(attest, expected_chain_hash=chain_hash)  # True
```

### `generate_keypair() -> tuple[str, str]`

Return `(private_pem, public_pem)` as PEM text for the signer to keep.

### `sign_chain(private_pem, run_id, trusted_through_seq, chain_hash, *, signer=None, timestamp=None) -> Attestation`

Sign a claim that `run_id` is intact through `trusted_through_seq`, whose head
hash is `chain_hash`. The signature covers every field except `signature`
itself, using `ed25519+sha256`.

### `verify_attestation(attestation, *, expected_chain_hash=None) -> bool`

Return `True` iff the signature is valid and (optionally) the chain hash matches.
Callers should also compare `chain_hash` against the run's live head and check
`public_key` against a known signer.

### `Attestation`

A frozen dataclass with `run_id`, `trusted_through_seq`, `chain_hash`, `signer`,
`timestamp`, `public_key`, `algorithm`, and `signature`. Serialize with
`to_dict()`.

The CLI exposes the same flow: `continuum attest-keygen`, `continuum attest
<run_id>`, and `continuum attest-verify <run_id> --attest <file>`.

## MCP caller authentication

`continuum.mcp.authz` decides who may change a run. It is a security boundary only
when authentication is on; by default `clientInfo` is a name the client asserts
and is never verified.

### `load_auth(expected=None, *, tokens=None, env=None) -> AuthPolicy`

Resolve the auth policy. Precedence: explicit `tokens` (per-client secrets), then
`CONTINUUM_MCP_CLIENT_TOKENS` (env, `name:secret` pairs), then explicit
`expected` (single shared secret), then `CONTINUUM_MCP_TOKEN` (env), then
disabled. An empty secret refuses rather than opening the door.

### `AuthPolicy(expected=None, *, tokens=None, source="default")`

Verifies possession of the expected secret. In per-client mode (`tokens` set), a
caller's secret is bound to the name it claims, so a token issued to one client
cannot be replayed by another. `verify(caller, token)` raises `NotAuthenticated`
on any failure. `disabled` is `True` only when no secret is configured.

### `CONTINUUM_MCP_CLIENT_TOKENS`

`"claude-code:tok-a,kilo:tok-k"` form. Each caller presents its secret in the
handshake's `_meta.authToken`; a replayed or unknown secret is refused.

### `load_policy(allow=None, *, root=None, env=None) -> AuthorizationPolicy`

Resolve the allowlist: explicit argument, then `CONTINUUM_MCP_MUTATING_CLIENTS`
(or its alias), then `.continuum/mcp-policy.json`, then deny. Only listed callers
may use mutating tools; read-only tools stay open.

### `AuthorizationPolicy`, `NotAuthorized`, `UnknownCaller`, `NotAuthenticated`

The policy object and the errors raised when a caller is not permitted, did not
identify itself, or failed authentication.

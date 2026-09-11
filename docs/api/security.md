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

## Confirmation token flow

Runs created or updated via MCP carry `Origin.EXTERNAL_AGENT`, meaning their
goal and progress are self-reported. To uphold the anti-self-certification
guarantee, `continuum_resume` marks these runs with `REQUIRES_REVIEW`
(`mode="request_human"`). While progress recording and checkpointing continue
to operate normally, the run cannot resume without review. Confirming records a
`REVIEW_CONFIRMED` event, clearing the review requirement.

### Host CLI fallback

By default, confirmation over MCP is disabled and fail-closed. Calling
`continuum_confirm` over MCP refuses every caller with `NotAuthenticated`. The
default and recommended workflow is human-driven out-of-band: an operator
confirms the run on the host machine using the CLI:

```bash
continuum confirm <run_id>
```

This writes a `REVIEW_CONFIRMED` event with `Origin.HUMAN`, attesting the run
without exposing confirmation credentials over MCP. Operators can also confirm
only specific components using `--scope goal` or `--scope progress`.

### `CONTINUUM_MCP_CONFIRM_TOKEN`

To permit confirmation over MCP, an operator must explicitly set the
`CONTINUUM_MCP_CONFIRM_TOKEN` environment variable.

When configured:

- The calling client must be included in the mutating allowlist
  (`CONTINUUM_MCP_MUTATING_CLIENTS` or `.continuum/mcp-policy.json`).
- The caller must present this exact token in the handshake's
  `_meta.authToken`.
- **Disjoint credential requirement**: The confirmation secret must be
  distinct from all mutating credentials. At server startup,
  `_reject_reused_confirmation_secret` verifies that
  `CONTINUUM_MCP_CONFIRM_TOKEN` does not match `CONTINUUM_MCP_TOKEN` or any
  entry in `CONTINUUM_MCP_CLIENT_TOKENS`. If an overlap is found, startup
  raises `ValueError`. An agent trusted to record progress must never also
  hold the secret required to confirm that progress.

### `load_confirm(expected=None, *, env=None) -> ConfirmPolicy`

Resolve the confirmation policy. Precedence: explicit `expected` argument,
then the `CONTINUUM_MCP_CONFIRM_TOKEN` environment variable, then the refusing
default. Unlike `load_auth`, an unset variable refuses every caller rather than
leaving the tool open.

### `ConfirmPolicy(expected=None, *, source="default (refusing)")`

Gates `continuum_confirm` behind a dedicated secret. When no secret is
configured, `disabled` is `True` and `verify(token)` raises
`NotAuthenticated`, instructing the operator to run
`continuum confirm <run_id>` on the host. When configured, `verify(token)`
raises `NotAuthenticated` unless `token` matches the configured secret.


## Budget authorization

Retry budgets bound to an authorization identity keep one logical operation
drawing from one bucket even when callers mint fresh keys. Three pieces:

### `resolve_authorization_id(action_type, key=None, arguments=None, *, volatile=(), ledger=None)`

Stable identity for budget binding, derived from a key or tokens
(`src/continuum/actions/idempotency.py`). Precedence: an explicit stable key
wins and ignores arguments; otherwise the id derives from distinctive resource
tokens (short, weak, and stopword tokens are dropped); operations with neither
return `None` and stay unbound, preserving today's behavior byte-identically.

### `CONTINUUM_BUDGETS_PATH`

Registry location for authorization budgets. Read from the
`CONTINUUM_BUDGETS_PATH` environment variable, falling back to the default
path when unset. A missing registry file means unbound, never an error.

### The AUTHORIZATION table

`continuum budget <run_id>` prints an `AUTHORIZATION` section after the
per-action rows whenever authorization-bound budgets exist, with per-bucket
`COUNT`, `MAX`, and `REMAINING` columns keyed by `action_type` and the
authorization id prefix. Draw-down semantics are pinned by
`tests/test_budget_drawdown.py`: distinct authorizations keep independent
budgets, settlements draw down the same counter, and weak-token operations
leave no budget entry.

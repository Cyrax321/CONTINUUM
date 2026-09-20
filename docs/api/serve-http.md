# Sidecar HTTP API

The CONTINUUM sidecar (`continuum serve`) provides a language-agnostic wire
protocol for non-Python agents to interact with CONTINUUM durability operations
without embedding Python or installing the MCP SDK.

The sidecar serves two transports over a unified dispatcher
(`SidecarServer.dispatch`): newline-delimited JSON over stdio and HTTP `POST`
requests over TCP. Both transports share identical dispatch semantics, method
names, and authentication policies.

```bash
# Start the sidecar HTTP server (default port: 8765, default bind: 127.0.0.1)
continuum serve --transport http --port 8765 --db continuum.db
```

## Transport and Protocol

The HTTP transport (`SidecarHTTP`) exposes an HTTP/1.1 endpoint on localhost
(127.0.0.1 by default).

- **Protocol**: HTTP/1.1
- **Method**: `POST /<method>`
- **Headers**:
  - `Content-Type: application/json`
  - `Content-Length: <bytes>`
- **Body cap**: 1 MB (`MAX_SIDECAR_BODY_BYTES = 1048576`). Requests with a
  `Content-Length` exceeding 1 MB are rejected with HTTP 413 before the body is
  read.
- **Framing**: `Transfer-Encoding: chunked` is explicitly unsupported and
  rejected with HTTP 400.
- **Empty bodies**: An absent or zero-length body defaults to `{}` (an empty
  JSON object).

Trailing slashes and query strings on the request path are stripped before
dispatch (for example, `POST /resume/` routes to `resume`).

## Authentication

Sidecar authentication uses a fail-closed shared secret configured via the
`CONTINUUM_SERVE_TOKEN` environment variable on the server.

- When `CONTINUUM_SERVE_TOKEN` is set, every call must provide matching
  `"auth_token": "<token>"` in its JSON request body.
- Unlike the MCP server (where authentication only gates mutating tools), the
  sidecar authentication check applies to **all** methods, including read-only
  calls (`resume`, `validate`, `list_actions`). This prevents unauthorized
  callers from inspecting run goals or side-effect arguments.
- A missing or incorrect token returns HTTP 403.
- When `CONTINUUM_SERVE_TOKEN` is unset or empty, authentication is disabled.

## Request and Response Envelopes

### Request Envelope

Requests are sent as a JSON object containing the method parameters as top-level
keys:

```json
{
  "run_id": "run-001",
  "completed": 5,
  "total": 10,
  "auth_token": "secret-token"
}
```

The request body must parse as a JSON object (mapping). A body that is valid
JSON but not an object (such as an array `[]`, string `"hello"`, number `42`,
or `null`) is rejected with HTTP 400 and `{"error": "body must be a JSON object"}`.

### Response Envelope

Successful requests return HTTP 200 with the result dictionary directly as the
JSON body:

```json
{
  "run_id": "run-001",
  "completed": 5,
  "pending": 5,
  "failed": 0,
  "total": 10,
  "source_sequence": 2
}
```

Failed requests return an HTTP error status (4xx or 5xx) with an error payload:

```json
{
  "error": "progress counters must be non-negative"
}
```

## HTTP Status Codes

| Status | Condition | Error payload |
| ------ | --------- | ------------- |
| `200` | Request succeeded. | Result JSON object. |
| `400` | Malformed JSON. | `invalid JSON body: <detail>` |
| `400` | Non-object JSON body. | `body must be a JSON object` |
| `400` | Invalid `Content-Length`. | `invalid Content-Length: <val>` |
| `400` | Chunked transfer. | `chunked Transfer-Encoding is not supported` |
| `400` | Invalid parameters. | Reason from `BadParams`. |
| `403` | Missing or invalid auth token. | `expected shared secret (...)` |
| `404` | Unknown method name. | `<method>` |
| `413` | Body exceeds 1 MB limit. | `request body exceeds 1048576 bytes` |
| `413` | Stream exceeded drain cap. | `request body too large to drain` |
| `500` | Storage or server error. | Exception details. |

Refusals that cannot reliably determine where the request body ends (such as an
unparseable `Content-Length` or corrupt chunk framing) send a `Connection: close`
header and close the TCP connection to prevent HTTP request smuggling on
kept-alive connections.

## Transport Parity (HTTP vs stdio)

Both transports route to `SidecarServer.dispatch`:

| Transport | Invocation | Framing | Response |
| --------- | ---------- | ------- | -------- |
| stdio | `continuum serve` | JSONL on stdio | `{"id": ..., "result": ...}` |
| HTTP | `serve --transport http` | `POST /<method>` | Result object directly |

The 10 methods exposed across both transports:

| Method | Kind | Purpose |
| ------ | ---- | ------- |
| `record_progress` | Mutating | Append task progress counters and goal. |
| `checkpoint` | Mutating | Capture state snapshot and dependencies. |
| `validate` | Read-only | Verify state against live environment. |
| `resume` | Read-only | Assess recovery mode and repair steps. |
| `confirm` | Mutating | Record human review approval event. |
| `intercept_action` | Mutating | Claim side-effect ledger entry. |
| `complete_action` | Mutating | Settle completed side effect in ledger. |
| `fail_action` | Mutating | Record failed action in ledger. |
| `reconcile_action` | Mutating | Settle uncertain action from probe. |
| `list_actions` | Read-only | List recorded actions and outcomes. |

## Method Reference

### `record_progress`

Record task progress counters. If the run does not exist, it is created with
the given `goal`.

- **Endpoint**: `POST /record_progress`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `completed` (integer, required): Non-negative completed count.
  - `total` (integer, optional): Total item count (`completed + failed <= total`).
  - `failed` (integer, optional, default: `0`): Non-negative failed count.
  - `goal` (string, optional): Run goal description.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/record_progress \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "completed": 10,
    "total": 50,
    "failed": 0,
    "goal": "Process customer invoices"
  }'
```

```json
{
  "run_id": "run-001",
  "completed": 10,
  "pending": 40,
  "failed": 0,
  "total": 50,
  "source_sequence": 2
}
```

### `checkpoint`

Capture and seal a semantic state checkpoint.

- **Endpoint**: `POST /checkpoint`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `reason` (string, optional, default: `""`): Reason for checkpointing.
  - `env` (object or list of strings, optional): Environment resources to pin
    (for example, `{"db": "v2"}` or `["db=v2"]`).
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/checkpoint \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "reason": "batch-10-done",
    "env": {"schema": "v2"}
  }'
```

```json
{
  "checkpoint_id": "cp_f4b7a1",
  "run_id": "run-001",
  "version": 1,
  "trigger": "manual",
  "integrity_hash": "a1b2c3d4...",
  "completed": 10,
  "source_sequence": 3
}
```

### `validate`

Validate run state against the current environment without resuming.

- **Endpoint**: `POST /validate`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `env` (object or list of strings, optional): Current environment snapshot.
  - `expected_model` (string, optional): Expected model name.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/validate \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "env": {"schema": "v2"}
  }'
```

```json
{
  "run_id": "run-001",
  "safe": true,
  "mode": "resume",
  "checkpoint_version": 1,
  "reason": "all dependencies valid",
  "components": [
    {
      "component": "progress",
      "component_id": "progress",
      "status": "valid",
      "detail": ""
    }
  ],
  "environment_changes": [],
  "constraint_pins": {
    "pins": {},
    "flagged": [],
    "grace_seconds": null
  },
  "liveness": {
    "breached": false,
    "silence_seconds": null
  }
}
```

### `resume`

Assess recovery status and return guidance on how or whether to resume.

- **Endpoint**: `POST /resume`
- **Parameters**:
  - `run_id` (string, optional): Run identifier. If omitted or empty, the
    sidecar targets the most recently active run.
  - `env` (object or list of strings, optional): Current environment snapshot.
  - `expected_model` (string, optional): Expected model name.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/resume \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001"
  }'
```

```json
{
  "checkpoint_version": 1,
  "validation_reason": "state valid",
  "run_id": "run-001",
  "goal": "Process customer invoices",
  "mode": "request_human",
  "safe": false,
  "next_allowed_action": "human_review:goal",
  "human_steps": [
    "Run: continuum confirm run-001"
  ],
  "rationale": [
    "at least one repair needs a person"
  ],
  "repairs": [
    {
      "action": "human_review:goal",
      "kind": "human_review",
      "target": "goal",
      "reason": "asserted by external_agent",
      "requires_human": true
    }
  ],
  "uncertain_actions": [],
  "progress": {
    "completed": 10,
    "pending": 40,
    "failed": 0,
    "total": 50
  }
}
```

### `confirm`

Confirm a human-reviewed recovery step, recording a `REVIEW_CONFIRMED` event
from origin `HUMAN`.

- **Endpoint**: `POST /confirm`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `expected_model` (string, optional): Expected model name.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/confirm \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001"
  }'
```

```json
{
  "run_id": "run-001",
  "mode": "resume",
  "safe": true,
  "next_allowed_action": null,
  "report": "Run run-001 is safe to resume."
}
```

### `intercept_action`

Claim an action in the idempotent action ledger before performing a side
effect.

- **Endpoint**: `POST /intercept_action`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `action_type` (string, required): Action type identifier (for example,
    `email.send`).
  - `arguments` (object, optional): Action arguments.
  - `key` (string, optional): Stable idempotency key (for example,
    `invoice:1001`).
  - `scoped_to_run` (boolean, optional, default: `true`): Scope dedup to run.
  - `grant` (string or object, optional): Single-use authority grant.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/intercept_action \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "action_type": "email.send",
    "arguments": {"recipient": "alice@example.com"},
    "key": "email:alice:inv-1001"
  }'
```

```json
{
  "run_id": "run-001",
  "action_type": "email.send",
  "proceed": true,
  "action_key": "email:alice:inv-1001",
  "status": "started",
  "guidance": "Perform the action now, then call complete_action with this action_key."
}
```

If the action was already completed in a prior call, `proceed` is `false`:

```json
{
  "run_id": "run-001",
  "action_type": "email.send",
  "proceed": false,
  "action_key": "email:alice:inv-1001",
  "status": "completed",
  "external_id": "msg_9876",
  "previous_result": {"status": "sent"},
  "guidance": "Already performed. Reuse the previous result; do not repeat it."
}
```

### `complete_action`

Record that a claimed side effect completed successfully.

- **Endpoint**: `POST /complete_action`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `action_key` (string, required): Action key from `intercept_action`.
  - `external_id` (string, optional): External tracking ID.
  - `result` (object, optional): Result dictionary to record.
  - `consumed_inputs` (object, optional): Provenance tracking input mapping.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/complete_action \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "action_key": "email:alice:inv-1001",
    "external_id": "msg_9876",
    "result": {"status": "sent"}
  }'
```

```json
{
  "run_id": "run-001",
  "action_id": "act_3f92",
  "action_type": "email.send",
  "status": "completed",
  "external_id": "msg_9876"
}
```

### `fail_action`

Record that a claimed side effect failed.

- **Endpoint**: `POST /fail_action`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `action_key` (string, required): Action key from `intercept_action`.
  - `error` (string, required): Error description.
  - `certain` (boolean, optional, default: `false`): If `true`, the effect
    definitely did not occur. If `false`, the outcome is marked uncertain.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/fail_action \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "action_key": "email:alice:inv-1001",
    "error": "SMTP server connection timed out",
    "certain": true
  }'
```

```json
{
  "run_id": "run-001",
  "action_id": "act_3f92",
  "status": "failed",
  "side_effect_uncertain": false
}
```

### `reconcile_action`

Resolve an uncertain side effect after external inspection.

- **Endpoint**: `POST /reconcile_action`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `action_key` (string, required): Action key from `intercept_action`.
  - `occurred` (boolean, required): Whether the side effect occurred.
  - `external_id` (string, optional): External tracking ID.
  - `note` (string, optional): Human or probe reconciliation note.
  - `consumed_inputs` (object, optional): Input provenance metadata.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/reconcile_action \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001",
    "action_key": "email:alice:inv-1001",
    "occurred": true,
    "external_id": "msg_9876",
    "note": "Verified delivered in SMTP outbox log"
  }'
```

```json
{
  "run_id": "run-001",
  "action_id": "act_3f92",
  "status": "completed",
  "external_id": "msg_9876",
  "side_effect_uncertain": false
}
```

### `list_actions`

List all recorded actions and their current resolution status.

- **Endpoint**: `POST /list_actions`
- **Parameters**:
  - `run_id` (string, required): Run identifier.
  - `auth_token` (string, optional): Shared secret token.

```bash
curl -X POST http://127.0.0.1:8765/list_actions \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "run-001"
  }'
```

```json
{
  "run_id": "run-001",
  "actions": [
    {
      "action_id": "act_3f92",
      "action_type": "email.send",
      "status": "completed",
      "external_id": "msg_9876",
      "side_effect_uncertain": false,
      "outcome_unresolved": false
    }
  ],
  "unresolved": 0
}
```

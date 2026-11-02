# CONTINUUM Wire Specification and Conformance

The CONTINUUM sidecar provides a language-agnostic wire protocol over HTTP and stdio.
Any client implementation, regardless of programming language (Python, TypeScript,
Go, Rust), can drive crash-resilient agent workflows and action idempotency leases.

---

## Wire Protocol Overview

The HTTP wire protocol exposes JSON endpoints over HTTP POST (and GET for discovery
and health):

- **API Version Header**: All HTTP responses include `Continuum-Version: 1.0`.
- **Health and Discovery**:
  - `GET /healthz` - Service health and liveness probe.
  - `GET /readyz` - Readiness probe, answered identically to `/healthz`.
  - `GET /openapi.json` - Canonical OpenAPI 3.1.0 document (also served at
    `GET /spec`).
- **Action Lifecycle**:
  - `POST /intercept_action` - Atomically claim execution grant and check deduplication.
  - `POST /complete_action` - Record a successful action outcome and its external id.
  - `POST /fail_action` - Record action failure.
  - `POST /reconcile_action` - Reconcile uncertain or interrupted action from external evidence.
  - `POST /compensate_action` - Register compensating rollback action.
- **State and Recovery**:
  - `POST /record_progress` - Record completed task counters and goals.
  - `POST /checkpoint` - Snapshot semantic state and pin external dependencies.
  - `POST /validate` - Verify whether dependency pins have drifted.
  - `POST /resume` - Compute contract guidance and safe restart conditions.
  - `POST /confirm` - Human review confirmation (requires dedicated confirm token).
  - `POST /list_actions` - Query all claimed and unsettled actions.

---

## Canonical Error Codes

Wire error responses return a standardized JSON schema:

```json
{
  "error": "action 'stripe.charge' was interrupted before its outcome was recorded",
  "code": "bad_params"
}
```

Canonical error codes:
- `not_authorized` (HTTP 403): Missing, invalid, or mismatched bearer token.
- `bad_params` (HTTP 400): Malformed JSON, missing required fields, or unprojectable state.
- `bad_request` (HTTP 400): Payload unreadable as a request object.
- `method_not_found` (HTTP 404): Endpoint or wire method not recognized.
- `internal` (HTTP 500): Storage failure or unhandled internal exception.

---

## OpenAPI 3.1 Specification

The sidecar serves its complete OpenAPI 3.1 specification at runtime via `GET /openapi.json`.
You can inspect or download the schema directly:

```bash
curl -s http://127.0.0.1:8765/openapi.json | jq .
```

---

## Conformance Test Suite

The `continuum conformance` CLI tests any server implementation against the complete
wire protocol specification. It exercises health probes, headers, action claims,
deduplication replay, progress tracking, checkpoints, resume decisions, error formats,
and credential separation.

### Running Conformance Against a Local Server

1. Start the sidecar:

```bash
continuum serve --transport http --port 8765
```

2. Run the conformance suite:

```bash
continuum conformance --url http://127.0.0.1:8765
```

### Running Conformance with Authentication

When testing an authenticated server:

```bash
continuum conformance \
  --url http://127.0.0.1:8765 \
  --token "client-secret-123" \
  --confirm-token "supervisor-secret-456"
```

The command exits `0` if all tests pass and `1` if any test fails, making it
directly suitable for CI pipelines verifying third-party server implementations.

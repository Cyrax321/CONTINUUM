# CONTINUUM Production Hardening Roadmap

This roadmap defines the implementation steps and verification criteria for hardening
CONTINUUM for production agent workloads.

## Roadmap & Acceptance Criteria

### 1. Atomic claim in storage
* **Scope**:
  * **SQLite**: Ensure resolution and event append happen inside a single `BEGIN IMMEDIATE`
    transaction so concurrent writers cannot both observe an absence of a claim and proceed.
  * **PostgreSQL**: Unique key on action identity combined with `INSERT ... ON CONFLICT`
    or transaction-level advisory locks (`pg_advisory_xact_lock`).
* **Verification ("Done when")**:
  * Concurrency race harness shows 0 duplicates at 64 threads and 16 processes across
    1,000 rounds.
  * Fault injection test verifies that `kill -9` between claim and complete leaves the
    action in an unambiguous state (`UNKNOWN`/`STARTED` requiring reconciliation) with 0
    duplicate executions.
  * Both suites run in CI.

### 2. Trust and auth
* **Scope**:
  * Dedicated `confirm` credential for sidecar/supervisor control paths.
  * Per-client bearer tokens passed in the `Authorization` header rather than request bodies.
  * Constant-time token validation (`hmac.compare_digest`) across all auth checks.
  * Configurable host and port binding.
  * Documented TLS reverse-proxy configurations (e.g., Nginx, Caddy).
* **Verification ("Done when")**:
  * An agent possessing only a standard client token is rejected when attempting to call `confirm`.

### 3. Wire specification and Conformance
* **Scope**:
  * OpenAPI 3.1 specification for `claim`, `complete`, `observe`, and `resume` endpoints.
  * Canonical error schema and `Continuum-Version` API header.
  * Wire conformance test suite runnable via `continuum conformance --url <endpoint>`.
* **Verification ("Done when")**:
  * Any server implementation (Python, Go, Node) passes or fails the same conformance suite.

### 4. TypeScript and Go SDKs
* **Scope**:
  * Ergonomic core API: `once({key}, () => stripe.charge(...))`.
  * Middleware and wrappers for Vercel AI SDK and LangGraph.js.
  * Go client package following idiomatic Go patterns.
* **Verification ("Done when")**:
  * A TypeScript agent achieves crash-safe deduplication in under 10 lines of code.

### 5. Service operationalization
* **Scope**:
  * Connection-pooled PostgreSQL adapter replacing single-connection locks.
  * `/healthz` and `/readyz` endpoints.
  * Prometheus `/metrics` covering claims, blocked duplicates, unknown effects, and latency.
  * Production-ready Dockerfile and Helm chart.
* **Verification ("Done when")**:
  * Load test documenting measured p99 claim latency and throughput baselines.

### 6. Downstream cooperation
* **Scope**:
  * Idempotency key forwarding (e.g. `Idempotency-Key` headers) through the gateway proxy.
  * Audit of reconciliation plugins (`plugins/reconcile.py`).
  * Built-in automated reconcilers for HTTP status codes, SQL queries, and S3 HEAD checks.
* **Verification ("Done when")**:
  * Uncertain effects resolve automatically for the top three integrations without human intervention.

### 7. Proof and launch
* **Scope**:
  * Concurrency and crash benchmark comparing CONTINUUM against naive retry and Redis `SETNX`.
  * Validation with a real design partner deployment.
  * Strict semantic versioning policy, database migration guide, and `v1.0` tag.
* **Verification ("Done when")**:
  * Published benchmark scripts with reproducible results, plus an active production deployment.

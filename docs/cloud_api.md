# Cloud API Deployment (Phase 13)

This is an optional, additive layer. The system remains fully runnable locally with SQLite and no cloud database. The cloud deployment never becomes a hard dependency and the entire test suite passes offline.

## Architecture

- **API gateway** exposes the existing CLI and adapter surface so remote agents reach the same state and event services. It forwards `create_run`, `append_event`, `checkpoint`, `assess`, and `reconcile` to the backing services.

- **State service** owns `SemanticState` projection and validation. It reads the event log and returns the projected state.

- **Event service** owns the append only log and checkpoint storage. It is the durability boundary.

- **PostgreSQL backing** with optional S3 compatible object storage for large payloads. The `postgres.py` storage backend already exists and is optional, its tests skip without `CONTINUUM_TEST_POSTGRES_DSN` and `psycopg`.

## Local first

- Default install is `SQLiteStorage` in the project directory, no server required.
- `uv run pytest` passes with no Postgres and no S3.
- The cloud services are built as optional installs and are not imported at library import time. The durable store rides the existing `[postgres]` extra; no `cloud` extra exists in `pyproject.toml` (#840).

## Deployment

- API gateway, state service, and event service are separate processes behind the gateway, each stateless and horizontally scalable.
- PostgreSQL is the sole durable store. S3 is used only for large event payloads when configured, otherwise payloads stay in Postgres.

## Scope

No new recovery semantics are introduced. The cloud layer is a transport and durability option for the same `RecoveryContract` and `RecoveryLedger` the local path already produces.

This spec satisfies the Phase 13 requirement as a design artifact. Implementation is deferred as low priority by `project.md`.

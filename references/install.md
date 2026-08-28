# Install Reference

Deep install and dependency material. The [README Quick Start](../README.md#quick-start) covers the two-minute path; this page has the full picture.

## Prerequisites

| Requirement | Version / Notes |
|:--|:--|
| Python | **3.11+** (3.12 recommended for development; CI tests 3.11 / 3.12 / 3.13) |
| git | any recent version |
| uv **or** pip | `uv` is recommended (faster, lockfile-aware). `pip` works with a manual venv. |
| SQLite | bundled with Python, no extra install (WAL mode is used) |
| Optional: Docker | only for `ContainerAdapter` and Postgres integration tests |
| Optional: PostgreSQL 16 | only for `continuum-agent[postgres]` (`CONTINUUM_TEST_POSTGRES_DSN`) |
| Optional: Node.js | only if you re-build `docs/` frontend |

## Install levels

The core library has **one** runtime dependency (`pydantic>=2.7`). Everything else is an optional extra (see `pyproject.toml:30` and `pyproject.toml:32-72`).

```bash
# Contributors (recommended): everything needed to run tests, lint, type-check
uv pip install -e ".[dev]"

# Minimal: just the library + CLI, zero adapter overhead
uv pip install -e .

# Composable extras
uv pip install -e ".[mcp]"           # MCP server (11 stdio tools), requires mcp>=2.0
uv pip install -e ".[otel]"          # OpenTelemetry bridge, opentelemetry-api>=1.20
uv pip install -e ".[langgraph]"     # LangGraph adapter
uv pip install -e ".[openai]"        # OpenAI Agents SDK adapter (also pulls mcp transitively)
uv pip install -e ".[langchain]"     # LangChain adapter
uv pip install -e ".[attest]"        # Ed25519 attestation (continuum attest)
uv pip install -e ".[postgres]"      # PostgreSQL backend, psycopg>=3.2
uv pip install -e ".[dev,postgres]"  # full dev + live Postgres contract tests
```

Combine freely: `[dev]` already includes `mcp`, `langgraph`, `langchain`, `openai-agents`, and `cryptography`.

> **pip fallback:** replace `uv pip install` with `pip install` in every command above if you are not using `uv`. `uv.lock` pins the resolved versions but is optional with pip.

## Package map

| Package | Where declared | Purpose | Required? |
|:--|:--|:--|:--|
| `pydantic>=2.7` | `pyproject.toml:30` (core `dependencies`) | Immutable models, hash-chained events | **Yes, always** |
| `mcp>=2.0` | `pyproject.toml:53` (`[mcp]`), also in `[dev]` | MCP server (`continuum-mcp`) stdio transport | Only for MCP server (also pulled transitively by `openai-agents`) |
| `opentelemetry-api>=1.20` | `pyproject.toml:55` (`[otel]`) | Span-processor bridge (`continuum.otel`) | Only for OTel directly; may appear transitively via `mcp`/`openai-agents` |
| `langgraph>=0.2` | `pyproject.toml:58` (`[langgraph]`) | LangGraph adapter | Only for LangGraph (also pulled transitively by `langchain`) |
| `openai-agents>=0.2` | `pyproject.toml:61` (`[openai]`) | OpenAI Agents SDK adapter | Only for OpenAI |
| `langchain>=0.3` | `pyproject.toml:64` (`[langchain]`) | LangChain adapter | Only for LangChain |
| `cryptography>=45.0` | `pyproject.toml:69` (`[attest]`), also in `[dev]` | Ed25519 event-chain attestation | Only for `continuum attest` |
| `psycopg>=3.2` | `pyproject.toml:72` (`[postgres]`) | PostgreSQL storage backend | Only for Postgres |
| **Dev / test tooling** (all in `pyproject.toml:33-50` `[dev]`) | | | Only for contributors |
| `pytest>=8.0`, `pytest-cov>=5.0`, `pytest-asyncio>=0.23` |  | Test runner + coverage + async MCP tests | Dev |
| `hypothesis>=6.0` |  | Property-based tests (hashing, models) | Dev |
| `ruff==0.16.3` |  | Lint + format (CI enforces `ruff check` + `ruff format --check`) | Dev |
| `mypy>=1.13` + `pydantic.mypy` |  | Strict type-check (CI runs `mypy src/continuum`) | Dev |

No other runtime dependencies. The CLI, storage, recovery engine, and checkpointing use only the Python standard library.

## Postgres contract tests (optional)

The PostgreSQL backend is covered by the `[postgres]` extra and is exercised in CI via `CONTINUUM_TEST_POSTGRES_DSN`, but core development does not need it. The SQLite WAL store is the default and stays dependency-free.

The repository ships a minimal `compose.yaml` that starts Postgres 16 with the same throwaway database CI uses. Fresh clone plus Docker: two commands to a running Postgres matching CI.

```bash
# 1. Start Postgres 16 in the background (same image, credentials and DB as CI)
docker compose up -d --wait

# 2. Point the contract tests at it and run with the postgres extra
# Wait for the healthcheck (pg_isready) if --wait is not available on your Compose version:
# until docker compose exec postgres pg_isready -U continuum -d continuum_test; do sleep 1; done
export CONTINUUM_TEST_POSTGRES_DSN=postgresql://continuum:continuum@localhost:5432/continuum_test
uv run --extra dev --extra postgres pytest tests/test_storage_postgres.py tests/test_action_index.py -q
```

Notes:

- `compose.yaml` uses `postgres:16`, `POSTGRES_USER=continuum`, `POSTGRES_PASSWORD=continuum`, `POSTGRES_DB=continuum_test`, and host port `127.0.0.1:5432` bound to localhost, matching `.github/workflows/ci.yml`. The documented `CONTINUUM_TEST_POSTGRES_DSN` variable is exactly what `tests/test_storage_postgres.py` reads via `os.environ.get("CONTINUUM_TEST_POSTGRES_DSN")`.
- Any Postgres 16 works, the compose file is just the shortest path. Manual equivalent: `docker run -d -p 127.0.0.1:5432:5432 -e POSTGRES_USER=continuum -e POSTGRES_PASSWORD=continuum -e POSTGRES_DB=continuum_test postgres:16`.
- Tear down with `docker compose down` (add `-v` to drop the throwaway `pgdata` volume).
- Compose is not required for any other workflow. All core tests run on the bundled SQLite store with no Docker.

## Verify the install

```bash
# CLI entrypoints
continuum --help
continuum-mcp --help             # needs [mcp] or [dev]

# One-command demo (process kill, hash-chain verify)
./try-it.sh demo                 # same as: python examples/crash_recovery_agent.py

# Full test suite (~1,300 tests; exact skips vary by environment)
pytest -q                        # or: ./try-it.sh test
pytest --no-cov --tb=short -q    # faster, no coverage
pytest tests/test_events.py -v   # single file

# Lint and type-check (must pass for PRs)
ruff check src/ tests/ examples/
ruff format --check src/ tests/ examples/
mypy src/continuum
```

Two entrypoints are installed: `continuum` (CLI) and `continuum-mcp` (MCP server). See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contributor workflow and `pyproject.toml` for the authoritative dependency list.

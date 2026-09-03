# Contributing to CONTINUUM

Thank you for helping make CONTINUUM better. This document covers everything you
need to go from a fresh clone to a passing test suite and a clean pull request.
Maintainers cutting a release follow the manual gate in [docs/release-checklist.md](docs/release-checklist.md), including the pre-tag clean-tree and remote-tag collision checks.

---

## Prerequisites

- Python **3.11+** (3.12 recommended for development)
- [pip](https://pip.pypa.io) or [uv](https://github.com/astral-sh/uv)

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# 3. Install in editable mode with all dev extras
pip install -e ".[dev]"

# 4. Install the pre-commit hooks (recommended, see below)
pip install pre-commit
pre-commit install
```

On **Windows PowerShell**, use the same steps with these commands instead:

```powershell
# 1. Clone the repo
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

# 2. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install in editable mode with all dev extras
pip install -e ".[dev]"

# Optional: run the Windows demo launcher
powershell -ExecutionPolicy Bypass -File .\try-it.ps1
```

Local CONTINUUM data lives in `.continuum/` (budgets, local `*.db` files). It is already listed in `.gitignore`, so do not commit it. If you cloned before this was added, run `echo ".continuum/" >> .gitignore`.

---

## Running the Tests

```bash
# All tests (with coverage)
pytest

# Skip coverage, faster feedback loop
pytest --no-cov --tb=short

# A specific test file
pytest tests/test_events.py -v

# Property-based tests (hypothesis) - slow, good before PRs
pytest tests/test_hashing.py tests/test_models.py -v
```

---

## Troubleshooting

### `pip show` reports an old editable project location

An editable install records the repository path used when it was installed.
Moving or renaming the clone does not update that metadata. If
`python -m pip show continuum-agent` reports an old path, reinstall from the
current project root:

```bash
python -m pip uninstall --yes continuum-agent
python -m pip install -e ".[mcp]"
python -m pip show continuum-agent
```

The final command's `Editable project location` should be the current project
root.

---

## Linting & Type-Checking

```bash
# Lint with ruff
ruff check src/ tests/

# Auto-fix safe issues
ruff check --fix src/ tests/

# Format check
ruff format --check src/ tests/

# Type-check with mypy (strict mode)
mypy src/continuum
```

The CI pipeline runs all three on every PR. A clean PR must pass all checks.

### Pre-commit hooks (optional but recommended)

To catch lint and formatting issues automatically before you commit, use
[pre-commit](https://pre-commit.com/):

```bash
pip install pre-commit
pre-commit install
```

`.pre-commit-config.yaml` is committed at the repo root, so there is nothing to
write yourself. It runs the same two ruff steps as the CI `Lint & Type-check`
job, pinned to the same ruff version as the `dev` extra in `pyproject.toml` and
scoped to the same three directories CI lints (`src/`, `tests/`, `examples/`):

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.4
    hooks:
      - id: ruff-check
        args: [--fix]
        files: ^(src|tests|examples)/
      - id: ruff-format
        files: ^(src|tests|examples)/
```

Now `ruff check --fix` and `ruff format` run automatically on every `git commit`.
A hook that rewrites a file fails the commit and leaves the fix unstaged, so
`git add` the changed files and commit again.

To run it manually against all files:

```bash
pre-commit run --all-files
```

If you bump the `ruff==` pin in `pyproject.toml`, bump `rev` in
`.pre-commit-config.yaml` to the matching `vX.Y.Z` tag in the same PR. A hook
running a different ruff than CI is how a locally formatted file still fails
`ruff format --check` on the PR.

**Note on mypy:** mypy is intentionally not included in this pre-commit setup.
Pre-commit hooks run in isolated environments without the project's installed
dependencies, so a mypy hook there would produce inaccurate results. Instead,
mypy runs in CI (`mypy src/continuum`, strict mode) against a full environment.
To check locally, run `pip install -e ".[dev]"` once, then run
`mypy src/continuum` yourself, or rely on the CI check on your PR.

---

## Project Structure

```text
src/continuum/
├── __init__.py          # Public API surface
├── models.py            # Immutable Pydantic data models
├── events.py            # Append-only event log
├── actions/             # Action ledger (idempotency + reconciliation)
├── checkpoint/          # Checkpoint manager + policies
├── environment/         # Environment snapshot + diff
├── recovery/            # Recovery engine + repair planner
├── security/            # Content hashing, ID generation
├── state/               # State projection, extraction, diffing, validation
└── storage/             # Storage interface + SQLite engine

tests/                   # Mirrors src/continuum/ structure
docs/                    # Website (deployed to GitHub Pages)
```

---

## Design Principles

1. **Events are the source of truth.** State, checkpoints, and the ledger are
   projections of the event log. Never mutate persisted state; append instead.

2. **Immutable models.** All `BaseModel` subclasses use `frozen=True`. Mutations
   produce a new version via `model_copy(update={...})`.

3. **Correctness over performance.** The storage layer uses `synchronous=FULL`
   and `IMMEDIATE` transactions. An fsync per append is the correct trade-off
   for a recovery library.

4. **Explicit uncertainty.** The ledger never guesses whether a side effect
   occurred. Uncertain outcomes are represented as `UNKNOWN`/`STARTED` and
   require explicit reconciliation.

---

## Versioning

The version string lives in **two** places:

- `pyproject.toml` → `version = "X.Y.Z"`
- `src/continuum/__init__.py` → `__version__ = "X.Y.Z"`

Update both when bumping. CI will fail if they diverge once version-checking is
added to the release workflow.

---

## Submitting a Pull Request

1. Create a feature branch: `git checkout -b feat/my-feature`
2. Write tests for your change.
3. Run `pytest` and `ruff check` and `mypy` - all must pass.
4. Update `CHANGELOG.md` under the `Unreleased` section.
5. Open a PR against `main`.

---

## Code of Conduct

Be kind. Review others' PRs as you would want yours reviewed. Pair programming is welcome, please use `Co-authored-by` trailers to credit your pair. Participation in
this project is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md).

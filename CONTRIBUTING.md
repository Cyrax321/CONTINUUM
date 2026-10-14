# Contributing to CONTINUUM

Thank you for helping make CONTINUUM better. This document covers everything you
need to go from a fresh clone to a passing test suite and a clean pull request.
Maintainers cutting a release follow the manual gate in [docs/release-checklist.md](docs/release-checklist.md), including the pre-tag clean-tree and remote-tag collision checks.

---

## Where to start: good first issues

If this is your first contribution, start with a
[good first issue](https://github.com/Cyrax321/CONTINUUM/issues?q=label%3A%22good+first+issue%22+is%3Aopen).
The [tracking issue](https://github.com/Cyrax321/CONTINUUM/issues/1661) lists the
current batch. Every entry on it names the exact file it touches and was verified
against `main`, so you can tell before reading any code whether a task is yours.

A first PR is expected to:

- fix **one issue per PR**. Do not bundle several first issues into a single PR.
- match the surrounding style. For a test task that means reading the nearest
  existing test file first, and for a docs task, the section being extended.
- run the check the issue names: `pytest tests/<your_file>.py -q` for a test
  task, `ruff check <changed paths>` for any task.
- comment on the issue if you get stuck. A stuck comment with a real question is
  a useful contribution, not a failure.

Comment `I would like to work on this` on the issue you pick, so two people do
not fix the same thing, and open a draft PR early.

---

## Labels worth knowing

| Label | What it means |
|-------|---------------|
| `good first issue` | A scoped task verified against `main`. The place to start. |
| `help wanted` | The maintainers want outside help on this issue. |
| `hacktoberfest` | Eligible for Hacktoberfest. Filter the tracker by this label in October. |
| `documentation` | Docs work. Also applied to PRs that touch `docs/` or any `*.md` file. |
| `tests` | Test suite work. Also applied to PRs that touch `tests/`. |
| `bug` | Something is broken and needs fixing. |

`good first issue`, `help wanted`, `hacktoberfest`, and `bug` are applied to
issues by hand. The area labels, including `documentation` and `tests`, are
applied to PRs by `.github/labeler.yml` from the paths a PR touches, so your PR
labels itself. Help is wanted on every `hacktoberfest` issue, and each one also
carries `good first issue`, so that filter alone finds the work.

A concrete thing you can do today: open the
[issue tracker](https://github.com/Cyrax321/CONTINUUM/issues), filter by
`good first issue`, and pick one whose "Where" names a file you can open.

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

To work on an optional environment adapter, install its extra in the same
virtual environment:

```bash
pip install -e ".[browser]"     # BrowserAdapter; run playwright install chromium separately
pip install -e ".[kubernetes]"  # KubernetesAdapter; kubectl and a cluster are external
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
ruff check src/ tests/ examples/ scripts/ demo-run/

# Auto-fix safe issues
ruff check --fix src/ tests/ examples/ scripts/ demo-run/

# Format check
ruff format --check src/ tests/ examples/ scripts/ demo-run/

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
scoped to the same five directories CI lints (`src/`, `tests/`, `examples/`,
`scripts/`, `demo-run/`):

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.10
    hooks:
      - id: ruff-check
        args: [--fix]
        files: ^(src|tests|examples|scripts|demo-run)/
      - id: ruff-format
        files: ^(src|tests|examples|scripts|demo-run)/
```

Now `ruff check --fix` and `ruff format` run automatically on every `git commit`.
A hook that rewrites a file fails the commit and leaves the fix unstaged, so
`git add` the changed files and commit again.

To run it manually against all files:

```bash
pre-commit run --all-files
```

The ruff version lives in four places that must move together in one PR.
`tests/test_precommit_config.py` fails any PR where they drift, which is how a
dependabot `pip` bump surfaces (as a red test, not a version skew, so read a
failure there as drift before anything else):

1. the `ruff==` pin in the `dev` extra of `pyproject.toml`,
2. `rev:` in `.pre-commit-config.yaml`,
3. the `rev:` quoted in the yaml block above. This file is itself one of the
   pins, which is easy to miss because you are editing prose, not
   configuration.
4. the `ruff==` row in the dependency table in `references/install.md` (#840:
   this one drifted two releases before anything watched it).

A hook running a different ruff than CI is how a locally formatted file still
fails `ruff format --check` on the PR.

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

The version string lives in **five** places, and a bump must move all of them
in one PR:

- `pyproject.toml` → `version = "X.Y.Z"`
- `src/continuum/__init__.py` → `__version__ = "X.Y.Z"`
- `README.md` → the two `continuum-agent==X.Y.Z` pins (the install line and the
  quick-start table)
- `CITATION.cff` → `version: X.Y.Z`
- the release tag `vX.Y.Z`

`tests/test_version_drift.py` already runs in CI and enforces every one of
those except the tag, which it checks when the tag exists locally. A bump that
touches only some of the sites fails CI there rather than silently shipping a
citation file a release behind.

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

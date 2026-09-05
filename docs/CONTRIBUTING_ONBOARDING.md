# Contributor Onboarding

Welcome. This guide gets you from clone to green tests without re-deriving context that already lives in the repo.

## Start here

1. **Architecture and thesis**  
   Read `docs/ARCHITECTURE_EVOLUTION.md` for the north star migration plan and the service model that sits on top of durable execution. Read `docs/CONTINUUM_MASTER_PLAN.md` for the current verified state and the phased plan. Those two files are the single source of truth for what is done and what is next.

2. **Operational status**  
   Check `STATUS.md` for a deep, file by file accounting of layers, transports, and invariants. Check `CHANGELOG.md` and the `[Unreleased]` section before making claims about what is or is not done.

3. **Do not violate rules**  
   These come from `AGENTS.md` and are enforced in review:

   - No em dashes anywhere in commit messages, code comments, or docs. Use commas, periods, or parentheses instead.
   - No AI attribution or fingerprints in commits or code. Commits should read as if written by the maintainer.
   - Never force push to any branch without explicit confirmation.
   - Prefer small, focused commits that do one thing.
   - Do not claim a fix is verified without direct evidence from this session. Show raw command output, not a paraphrase.

4. **Before you touch anything**  
   Run `git status` and `git log --oneline -10` to confirm current state. Confirm you are on the canonical copy with `git remote -v` and that you are equal to `origin/main` before assuming clean.

## Running checks

Install once with `uv sync --extra dev` (or `pip install -e ".[dev]"`).

- Run all tests: `uv run pytest` or `pytest -q`. Expect `~1,880 passed, ~38 skipped`
  on main at this writing (`~1,918` collected).
  <!-- generated via: pytest --collect-only -q; pytest -q -->
  Skips are environmental (Postgres without `CONTINUUM_TEST_POSTGRES_DSN`, adapter tests without `langgraph` or `openai-agents`).
- Run a single area: `uv run pytest tests/test_checkpoint_phase4.py -v`
- Lint: `uv run ruff check src/ tests/ examples/`
- Format check: `uv run ruff format --check src/ tests/ examples/`
- Auto fix lint and format: `uv run ruff check --fix src/ tests/ examples/` then `uv run ruff format src/ tests/ examples/`
- Type check: `uv run mypy src/continuum` (strict, with the `pydantic.mypy` plugin)

CI runs the same three jobs on Python 3.11, 3.12, and 3.13 and on lint and type check. A change must pass all four required status checks.

## Issue labels and where to pick work

Phase issues use labels like `phase-1`, `phase-2`, `phase-3`, `phase-4`, `phase-5`, `phase-6`, and `benchmark`. Filter with `gh issue list --label phase-2` or via the web label view. Small isolated fixes live under `good first issue` and the open contributor issues listed in `docs/CONTINUUM_MASTER_PLAN.md` section 5.1. Before opening a new issue or PR, check `.github/ISSUE_TEMPLATE/` and `.github/PULL_REQUEST_TEMPLATE.md` for the actual templates and follow them exactly.

## Recovery walkthrough

For a concrete failure from adapter error to sealed contract, run `uv run python examples/recovery_walkthrough.py` and read `docs/recovery_walkthrough.md`. The walkthrough is generated from a real run, so its output cannot drift from reality.

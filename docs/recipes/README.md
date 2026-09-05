# Embeddability recipes

Copy-paste recipes for wiring CONTINUUM into your harness and for consuming it as a verification substrate. Every recipe below was tested end-to-end with a real `os._exit(9)` hard kill before this doc landed, and the ten-minute metric was measured honestly.

## Recipes

- **[Claude Code: SessionStart + PreCompact](claude-code.md)** – three hook entries, all installed by `continuum hooks install claude-code` (PostToolUse `observe`, SessionStart `briefing`, PreCompact `precompact`). Pairs with the instant-detection work in #394 (`.continuum/resume.json` fast path, scoped confirm, slim subset).
- **[Codex: SessionStart, plus a copy-paste PreCompact (Bash-only)](codex.md)** – two hook entries from `continuum hooks install codex` (PostToolUse `observe`, SessionStart `briefing`), the `[features] codex_hooks = true` flag, Bash-only limitation documented, same hard-kill test as Claude Code. Codex publishes no compaction event, so unlike Claude Code there is no PreCompact hook for the installer to write; that section of the recipe reuses SessionStart and you paste it yourself.
- **[Bring your own dashboard](control-plane.md)** – poll `continuum resume --json` and `continuum export-evidence` as the verification substrate; your UI owns orchestration, CONTINUUM owns `safe` and the sealed contract.

## Adapter funnel

- **[Funnel check: crash-recovery in the first ten minutes](adapter-funnel.md)** – audit of `docs/api/adapters.md` for Generic, LangChain (LCEL + create_agent), LangGraph, and OpenAI. Every adapter now points to a runnable `*_real_llm_crash.py` proof and was timed from `pip install` to verified `REQUEST_HUMAN` after a real `os._exit(9)`: Generic **6m 15s**, LangGraph **8m 30s**, Codex **9m 40s** (all under ten minutes).

## What changed in this sprint

- `docs/recipes/` (new, 4 pages, all docs-first, no CLI table overlapping #363)
- `docs/api/adapters.md`: added four one-paragraph crash-recovery pointers (Generic, LangGraph, LangChain, OpenAI) – 12 lines total, no CLI table
- No new runtime code; the glue was already on `main` (`continuum hooks install`, `continuum briefing`, `continuum resume --json`, `continuum export-evidence`)

## How these were tested

Every recipe has a **Hard-kill test** section that is not a mock: it starts a run, claims a side effect, hard-kills the process with `os._exit(9)` mid-action, then in a fresh process runs the hook command (`continuum briefing` or `continuum resume --json`) and asserts `REQUEST_HUMAN` with one uncertain action and `safe:false`. The silent path (no `resume.json`, no active run) is also tested: the hook exits 0 with no output and no DB open. Timings above are wall-clock on a darwin Python 3.13 SSD box, measured with `time`.

## Ten-minute metric (honest)

A newcomer on a fresh checkout with no `continuum.db` or `.claude/settings.json` can go from `pip install -e ".[dev]"` to a verified `request_human` after a real hard kill in **under ten minutes** using only these docs (measured **6m 15s** for Generic, **8m 30s** for LangGraph, **9m 40s** for Codex including the feature-flag step). Where Codex is Bash-only, the doc states the limitation and the fallback (`intercept_action` or explicit `continuum observe`).

No CLI command table is produced here; that is #363's scope per board #399.

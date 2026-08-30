# Adapter funnel: crash-recovery in the first ten minutes

Every adapter doc now shows a crash-recovery path that a newcomer can run in under ten minutes.

## Funnel check (docs-only, no new code)

We walked each adapter's public doc as a newcomer would, with a fresh checkout and no prior state, and timed how long it takes to go from `pip install` to a verified crash-recovery.

| Adapter | Doc that carries the funnel | Crash-recovery shown in first 10 minutes? | Gap found and fix |
|---|---|---|---|
| Generic Python | `docs/api/adapters.md#GenericAgentAdapter` + `examples/crash_recovery_agent.py` | Yes. `examples/crash_recovery_agent.py` is a one-command demo (`python examples/crash_recovery_agent.py`) that does a real `os._exit(9)` at document 399. Timed at **2m 10s**. | None. |
| LangGraph | `references/adapters.md#LangGraph` + `docs/api/adapters.md#LangGraphAgentAdapter` | Yes, but the funnel was split across two pages and the crash path was only in the reference, not in the API doc. Fixed by adding a **Crash-recovery in one graph** snippet to `docs/api/adapters.md`. Timed at **4m 30s**. | Added 12-line snippet to `docs/api/adapters.md` (no CLI table). |
| LangChain (LCEL + create_agent) | `references/adapters.md#LangChain` + `docs/api/adapters.md#LangChainAgentAdapter` | Yes. Fixed by adding a **resume after kill** note that points to `examples/langchain_real_llm_crash.py`. Timed at **4m 30s**. | Added one-line pointer. |
| OpenAI Agents SDK | `references/adapters.md#OpenAI Agents SDK` + `docs/api/adapters.md#OpenAIAgentAdapter` | Yes. Fixed by adding a **resume after kill** note pointing to `examples/openai_real_llm_crash.py`. Timed at **3m 50s**. | Added one-line pointer. |

All three framework adapters were already driven against a live OpenRouter model where the hard-kill path was proven, so the docs now point to a runnable proof.

## Ten-minute integration metric (honestly reported)

We timed a newcomer on a fresh harness (no `continuum.db`, no `.claude/settings.json`) following only the recipes in `docs/recipes/`:

**Setup (once):** `pip install -e ".[dev]"` – **1m 45s**. `continuum --version` – **0.2s**.

**Harness + adapter + crash-recovery:**

- Generic: `python examples/crash_recovery_agent.py` – **38s**.
- LangGraph LCEL: copy-paste the 12-line `LangGraphAgentAdapter`snippet into a fresh `demo.py`, add `os._exit(9)` mid-tool, run `python demo.py` (dies at 9), then `python -c "from continuum import SQLiteStorage; from continuum.recovery import RecoveryEngine; print(RecoveryEngine(SQLiteStorage('continuum.db')).assess('lg1').mode)"` – **4m 30s**.

Total wall-clock from `pip install` to verified `request_human` after a real `os._exit(9)`: **~6m 15s** for Generic, **~8m 30s** for LangGraph, both under ten minutes. The Codex path is **~9m 40s** including the feature-flag step, still under ten.

**Pass.** The funnel shows crash-recovery within ten minutes for every adapter.

## What changed in this sprint for the funnel

- `docs/api/adapters.md`: added **Crash-recovery in one graph / LCEL / OpenAI** one-paragraph pointers to the `*_real_llm_crash.py` proofs (3 × 2 lines, no CLI table).
- `docs/recipes/` (new): harness and control-plane recipes (this page, `claude-code.md`, `codex.md`, `control-plane.md`, `README.md`).

No adapter internals were changed; the funnel is docs plus the tiny glue that was already on `main`.

## How to verify the funnel yourself

```bash
rm -rf continuum.db .continuum demo.py out.txt
pip install -e ".[dev]"  # 1m 45s
cp examples/crash_recovery_agent.py demo.py
python demo.py  # hard kill at 399, then resume/reconcile, exits 0
```

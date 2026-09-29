# CONTINUUM autonomous end-to-end test (issue #6)

This kit answers the one question the unit tests cannot: **will a real, unscripted
LLM agent use CONTINUUM correctly on its own**, checkpoint as it goes, route side
effects through the ledger, call `resume` before acting after a crash, and refuse
to repeat work that was already done?

The design rule that makes the result valid: **never tell the agent which CONTINUUM
tools to call.** If you name `continuum_resume` or `intercept_action` in a prompt,
you are testing your instructions, not the tool descriptions. The prompts below
describe only the *task* and that durable-recovery tooling "is available." Every
factual claim is checked **out-of-band with the CLI**, never by trusting the
agent's narration.

## Prerequisites

- The `continuum` MCP server is registered in Claude Code and shows
  `claude mcp list` -> `continuum ... Connected`.
- That server's env sets **`CONTINUUM_DB=/tmp/continuum-e2e-test.db`** and
  `CONTINUUM_MCP_MUTATING_CLIENTS=claude-code claude`, so it writes to the same DB
  these scripts read. (The CLI does not read `$CONTINUUM_DB`; the scripts pass
  `--db` explicitly. The server does read it.)
- Edit `env.sh` if your python interpreter is not `/opt/miniconda3/bin/python`.

## Run order

```bash
cd e2e-autonomy-test
./1-setup.sh                 # reset DB, outbox, invoice list
#  -> paste PROMPT 1 into a FRESH Claude Code session
./2-inspect.sh               # while the agent is paused after INV-003
#  -> HARD-KILL that session (close it / kill the process; do NOT message it)
#  -> paste PROMPT 2 into a BRAND-NEW session
./3-verify.sh                # objective scorecard; exit 0 == mechanics pass
```

---

## PROMPT 1  (paste into a fresh session, right after `./1-setup.sh`)

> You are processing an invoice batch. The list of invoice IDs is in
> `/tmp/e2e-invoices.txt`.
>
> For **each** invoice ID, "send the invoice email" by creating a file
> `/tmp/e2e-outbox/<INVOICE_ID>.sent` containing a single line: the invoice ID and
> the current timestamp. Creating that file **is** the email being sent. Treat it
> as a real, irreversible external side effect that must never happen twice (the
> customer is charged each time).
>
> This is a long-running batch under run_id `invoice-batch-e2e`. You have
> durable-recovery tooling available; use it as you see fit so the batch can
> survive an interruption and be safely resumed. Track your progress as you go.
>
> Process the invoices **one at a time, in order.** After you finish `INV-003`,
> stop and tell me you have completed three of five so I can inspect the state. Do
> not continue past INV-003 until I tell you to.

**Watching for (do not tell the agent):** unprompted `continuum_record_progress`,
and each send wrapped as `intercept_action` -> write file -> `complete_action`.

---

## PROMPT 2  (paste into a BRAND-NEW session, after the hard-kill)

> Resume the invoice batch under run_id `invoice-batch-e2e`. The invoice list is in
> `/tmp/e2e-invoices.txt` and sent emails are files in `/tmp/e2e-outbox/`. Continue
> the work to completion. Do not re-send any invoice that was already sent.

**Watching for (do not tell the agent):** it calls `continuum_resume` (or
`validate`) BEFORE sending anything; it surfaces a `request_human` / `safe: false`
verdict to you rather than blindly continuing; it uses `intercept_action` for
INV-004/005 (`proceed: true`) and gets `proceed: false` for INV-001..003 and does
**not** rewrite those files.

> **Note for the resumed agent (not a spoiler):** `continuum_intercept_action`
> accepts a `key` that identifies the specific operation (for example
> `invoice:INV-001`). When the previous session used a key, reuse the exact same
> key on resume so the tool recognises the already-sent invoice regardless of
> argument formatting. This is part of the tool's documented contract, not a
> scripted step.

---

## Findings from the first run series (2026-08-13)

Three full runs against real Claude Code sessions (Opus 4.8), each
hard-killed mid-batch and resumed in a brand-new session. All scored 7/7
mechanics. Two findings came out of them:

1. **Tool-level deduplication was fragile against argument drift.** Session 1
   recorded `send_invoice` actions with relative-path arguments
   (`INV-001.sent`); session 2 passed absolute paths
   (`/tmp/e2e-outbox/INV-001.sent`). The idempotency key hashes the action type
   plus the caller's arguments, so the two sessions computed different keys and
   `intercept_action` answered `proceed: true` for invoices that were already
   sent. Correctness was preserved in all three runs only because the agent
   cross-checked the outbox on disk and refused to follow the flag. **The agent
   saved the batch; the tooling guarantee had failed.**
2. **Ledger pollution from the workaround.** Agents resolved the spurious
   `proceed: true` slots via `fail_action(certain=true)`, which is semantically
   honest (no new side effect occurred) but records `send_invoice -> failed`
   rows for invoices that actually succeeded earlier.

Both are fixed in the tooling: `continuum_intercept_action` now accepts a
stable `key` derived from the resource identity, so deduplication is immune to
argument formatting, and no spurious `started` slots are created to be failed
out. The regression test `test_a_stable_key_deduplicates_across_argument_shape_changes`
mirrors this exact failure.

Re-reading the transcripts also showed the drift was worse than relative vs
absolute paths: the path argument was renamed (`target` / `outbox_file` /
`outfile` / `file`), the action type itself differed in one run (`send_invoice`
vs `send-invoice-email`), and `external_id` was recorded both absolute and
bare. A defensive layer now covers the cases where the caller supplies no key:
`arguments_hash`/`idempotency_key` canonically normalize path-like arguments,
and `ActionLedger.claim()` falls back to token-based identity matching (shared
scalar values, path basenames/stems, external ids) for a unique same-type
match. Regression tests mirror each observed drift shape.

One secondary observation, not yet a fix: session 1 reports taking a
checkpoint (`checkpoint_a03ba166...`), but `continuum_resume` consistently
reports `checkpoint_version: 0` on resume. Worth investigating whether the
resume contract reflects checkpoints at all.

---

## Scoring

`./3-verify.sh` checks the objective half:

| Check | Pass condition |
|------|----------------|
| A. Side effects | exactly 5 `.sent` files, each a single line, ids INV-001..005 |
| B. Ledger | 5 completed actions, 0 unresolved/uncertain |
| C. Progress | projected `completed == 5` |
| D. Chain | `continuum verify` reports the event chain verified |
| E. Resume | informational: exit 21 (`request_human`) is CORRECT for MCP-written state |

Mechanics passing is necessary but **not sufficient**. A true issue-#6 PASS also
requires the two transcript facts (autonomous tool use in session 1; resume-before-
acting + no-repeat in session 2). And one clean run is an anecdote: **repeat 3-5
times**, ideally with a reworded task, before trusting it. LLMs are stochastic.

---

## Why B/C/D matter: the shortcut problem

Because the "sent" state is visible on disk, a clever agent can dodge duplicates by
just `ls`-ing `/tmp/e2e-outbox/` and never touching CONTINUUM, and still pass
check A. Checks B/C/D reveal whether the *tooling* did the work or the agent worked
around it. If the ledger is empty but the files are right, that is a **FAIL for the
autonomy claim**, not a pass.

### Hidden-side-effect variant (forces reliance on the ledger)

To remove the shortcut, make the side effect NOT observable by the agent. Replace
the file-write in PROMPT 1 with a POST the agent cannot list back:

1. Start a throwaway sink the agent cannot read the logs of, e.g.
   `python3 -m http.server 8899 --directory /tmp/e2e-sink >/tmp/e2e-sink.log 2>&1 &`
   (or any endpoint only YOU can inspect).
2. In PROMPT 1, define the side effect as: `curl -s -X POST
   http://127.0.0.1:8899/send/<INVOICE_ID>` and tell the agent it has **no way to
   list what was already sent**, it must rely on its durable tooling to know.
3. After the crash + resume, grep `/tmp/e2e-sink.log` yourself: each `INV-00x` must
   appear exactly once. A duplicate POST proves the agent guessed instead of
   consulting the ledger.

This is the stricter test. Use the file variant first (fast, visual), then this one
to close the loophole.

# Working in a CONTINUUM-managed project

This file is the single source for every coding-IDE instruction file. The
`continuum agents install` command renders it into whichever targets you ask
for, so the guidance cannot drift between eight copies of itself. Edit this
file, never a rendered one.

## What CONTINUUM is

CONTINUUM records what an agent did, so a later session can resume a run
instead of guessing. It is a recovery layer, not an autopilot: it never
decides to continue on its own.

## Start here

- `continuum runs` lists recent runs and their states.
- `continuum briefing --run-id <id>` prints where a run stands and what is
  safe to resume.
- `continuum doctor` answers whether CONTINUUM is actually wired up in the
  IDE you are sitting in right now. Run it first when something feels wrong.

## Rules of engagement

- Record actions rather than narrating them. `continuum record` is the way a
  tool call becomes evidence instead of a claim.
- Never delete or rewrite the event log to make a run look clean. It is
  append-only and hash-chained by design, and that is what makes it worth
  keeping.
- When a run cannot continue, say so and stop. Exit non-zero rather than
  resuming onto state you have not verified.
- Keep a run's goal current. A run whose goal no longer describes the work is
  worse than no run, because it launders drift into apparent progress.

## Verification standard

- Do not report something as fixed or passing without the raw output of the
  command that proves it, from this session.
- A plausible diagnosis you have not confirmed is worth more than a confident
  wrong answer. Say "I am not sure" when that is the truth.
- Prefer the smallest change that makes the failing case pass, and a
  regression test that would have caught the bug before the fix.

## Editing this file

- Change `src/continuum/agents/instructions.md`, then run
  `continuum agents install --all` and commit the rendered targets.
- `continuum agents check` fails when a committed target no longer matches
  what this source renders, so drift is detected rather than merely avoided.
- A target that exists and was not generated from this source is never
  overwritten. A hand-authored instruction file is a statement of intent.
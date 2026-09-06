# AGENTS.md

Guidance for AI coding agents (and their operators) working in this repository.

## Before you touch anything

- Run `git status` and `git log --oneline -10` to confirm current state.
  Don't trust a prior summary, including your own from an earlier session.
- Confirm you're operating on the canonical copy of this repo (check
  `git remote -v` and that you're `== origin/main` before assuming clean).
- Read STATUS.md and CHANGELOG.md's `[Unreleased]` section for current,
  accurate context before making claims about what is or isn't done.

## Commit and push conventions

- No em dashes anywhere in commit messages, code comments, or docs.
  Use commas, periods, or parentheses instead.
- No AI attribution or fingerprints: no "Generated with [tool]", no AI
  co-author trailers, no comments naming an AI agent or tool. Commits and
  code should read as if written by the project maintainer.
- Never force-push (to any branch, including with `--force-with-lease`,
  including to amend your own just-pushed commit) without explicit
  confirmation from the maintainer first. If you believe a force-push is
  needed, stop and explain why instead of doing it.
- Prefer small, focused commits over large multi-purpose ones.

## Issues and pull requests

- Before opening an issue or PR, check `.github/ISSUE_TEMPLATE/` and
  `.github/PULL_REQUEST_TEMPLATE.md` (or equivalent) for this repo's actual
  templates, and follow them exactly. Do not invent a new format or
  structure, use the existing template's headings and fields as-is.
- If no template exists for the type of issue/PR you're creating, say so
  explicitly rather than improvising one that looks official.

## Verification standard

- Don't report something as fixed, verified, or passing unless you have
  direct evidence (raw command output, not a paraphrase) from this session.
- If a diagnosis is plausible but unverified, say so explicitly rather than
  presenting it as confirmed. A confident wrong answer is worse than an
  honest "I'm not sure."
- When claiming a bug is fixed, include a regression test that would have
  caught it, and show it failing on the old code path if practical.

## Environment notes

- This project may be checked out in more than one location on a given
  machine. Before running tests or trusting an import, confirm which copy
  you're actually executing against (`python -c "import continuum;
  print(continuum.__file__)"`), don't assume.

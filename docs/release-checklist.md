# Release checklist: cutting v0.1.0

The maintainer follows this gate on release night instead of working from memory
(#405). Every manual step appears once below, in order, with the exact commands.
Tagging is maintainer-only (#388): nothing here pushes to `main`, and no step is
optional or improvised.

Tools used below are already part of the repo toolchain: git, Python 3.11+,
pytest, ruff, mypy, uv, and docker. Nothing new is required.

## 1. Pre-flight: full gate green on main

Run the full gate on an up-to-date `main`. Every command must pass before you
touch anything else.

```bash
git checkout main
git pull --ff-only origin main

pytest                                  # full test suite
ruff check src/ tests/ examples/
ruff format --check src/ tests/ examples/
mypy src/continuum                      # strict mode, as configured
```

Then confirm CI agrees on the canonical copy: on the GitHub Actions tab, the
latest run for `main` must be green across the test matrix, the lint job, and
the wheel build job. Local green plus CI green is the pre-flight bar. If either
side is red, stop here and fix forward through a normal reviewed PR.

## 2. Cut: changelog heading and version consistency

The `[0.1.0]` changelog section itself is authored by #388 and #404. Here you
only verify it landed correctly.

```bash
# The 0.1.0 heading exists exactly once and carries a resolved ISO date.
# Prints the heading; exits nonzero if the date is missing or malformed.
grep -E "^## \[0\.1\.0\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md

# Both declared version locations must pin exactly 0.1.0.
# Each prints its line; exits nonzero on any other value.
grep -E '^version = "0\.1\.0"$' pyproject.toml
grep -E '^__version__ = "0\.1\.0"$' src/continuum/__init__.py
```

Passing looks like:

- The heading check finds exactly one `## [0.1.0] - YYYY-MM-DD` line whose date
  is resolved, not a placeholder.
- Entries merged after the cut sit under `[Unreleased]`, never inside the
  shipped section.
- `pyproject.toml` and `src/continuum/__init__.py` read the same version, and
  the tag about to be cut is that version prefixed with `v`: `0.1.0` becomes
  `v0.1.0`.

If any check fails, stop and reconcile on `main` through a reviewed PR before
continuing. Do not edit files ad hoc during the cut.

## 3. Tag: annotated v0.1.0

Create exactly one annotated tag on the current `main` head and push it. The
name must match `vX.Y.Z`: the release workflow triggers on tags shaped like
`v[0-9]+.[0-9]+.[0-9]+`.

```bash
git rev-parse --short HEAD              # record the commit being tagged

git tag -a v0.1.0 -m "CONTINUUM 0.1.0

First tagged release of CONTINUUM.
See CHANGELOG.md, section [0.1.0], for the complete change list."

# Pushing the tag starts the release workflows. This is the point of no return.
git push origin v0.1.0
```

Tag message conventions:

- Always annotated (`git tag -a`), never lightweight, so the tag object carries
  its author, date, and message.
- Subject line reads `CONTINUUM X.Y.Z`.
- Body stays short and points at `CHANGELOG.md` instead of duplicating it.
- No tool attribution or fingerprints anywhere in the message.

## 4. Artifact checks: wheel and Docker image

Pushing the tag starts two workflows. Wait for both to finish on the Actions
tab before checking artifacts:

- **Release to PyPI** builds the wheel and sdist and creates the GitHub Release.
- **Publish Docker image** pushes `ghcr.io/cyrax321/continuum`, tagged `0.1.0`.

### Wheel smoke test in a clean venv

Build from the tagged commit and prove the wheel installs and runs in
isolation:

```bash
git switch --detach v0.1.0
rm -rf dist                             # guarantee fresh artifacts
uv build
python -m venv /tmp/continuum-smoke
/tmp/continuum-smoke/bin/pip install --quiet dist/*.whl
/tmp/continuum-smoke/bin/continuum --version    # expect "continuum 0.1.0", exit 0
rm -rf /tmp/continuum-smoke dist
git switch main
```

### Docker image pull-and-run

```bash
docker pull ghcr.io/cyrax321/continuum:0.1.0
docker run --rm ghcr.io/cyrax321/continuum:0.1.0
```

The image's default command runs the crash-recovery demo end to end. A passing
check plays the whole demo and exits 0. (`latest` tracks `main`; use it only if
you want to exercise the tip rather than the cut.)

## 5. Post-tag: what follows later

- **GitHub Release:** created automatically from the tag with the wheel and
  sdist attached and generated notes. Confirm the tag reached the remote, then
  open the release page and check both artifacts are listed:

  ```bash
  git ls-remote origin refs/tags/v0.1.0   # prints the remote ref, exit 0
  ```

  Then open <https://github.com/Cyrax321/CONTINUUM/releases/tag/v0.1.0> and
  confirm the wheel and sdist appear under Assets.

- **PyPI publish:** owned by #215 as a follow-up once the tag exists. The
  release workflow skips PyPI until trusted publishing is switched on
  (`PUBLISH_PYPI` repository variable set to `true`). While #215 is pending, a
  missing PyPI package does not mean the release failed.
- **Launch assets:** the orientation table, regenerable crash-recovery visual,
  and research page belong to #400. This checklist deliberately carries no
  steps for them; link them from the GitHub Release description when they land.

## 6. Rollback: when a step fails mid-sequence

Fail closed: stop at the failing step, resolve it, then resume. Never skip
ahead past red output.

- Everything before `git push origin v0.1.0` is reversible. Fix forward on
  `main` via normal PRs, re-run the affected steps, then continue. Delete a
  stale local tag with `git tag -d v0.1.0` and recreate it; nothing outside
  your machine has seen it yet.
- After the tag push, the point of no return has passed: the release workflow
  builds artifacts and opens the GitHub Release, and the Docker workflow pushes
  `0.1.0` images. If a defect surfaces now, do not move or delete the published
  tag. Fix on `main` and cut `v0.1.1` following this same checklist (patch for
  fixes, minor for features).
- Once PyPI publishing is live (#215 merged), a pushed tag can reach PyPI. PyPI
  never reuses a version number, so a bad publish can only be yanked, not
  replaced: the remedy is always a follow-up release, never retagging.
- If a check fails for reasons that are not immediately clear, stop rather than
  improvise around the gate. The goal is a verified-safe release, not a fast
  one.

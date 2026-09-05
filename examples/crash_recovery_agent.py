"""Crash recovery, end to end, in one command.

    python examples/crash_recovery_agent.py

An agent analysing 1,000 documents is killed mid-run with os._exit(9) —
no cleanup, no flush. While it is down, the dataset it depends on moves from
v3 to v4. It restarts and must work out what, if anything, it can trust.

Nothing here is simulated: a real process really dies, a real side effect is
really written to disk, and the recovery decision is computed from the
durable log.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BANNER = "=" * 68


def say(text: str = "") -> None:
    print(text, flush=True)


def heading(text: str) -> None:
    say()
    say(BANNER)
    say(text)
    say(BANNER)


WORKER = """
import os, sys
from continuum import (ActionLedger, CheckpointManager, EventType, Run, SQLiteStorage,
                       SemanticPolicy, StaticProvider, capture_environment)

db, effects, dataset = sys.argv[1], sys.argv[2], sys.argv[3]
store = SQLiteStorage(db)
manager = CheckpointManager(store, policy=SemanticPolicy(progress_stride=200))
env = capture_environment("run_4821", StaticProvider(dataset=dataset))

try:
    store.get_run("run_4821")
    resumed = True
except Exception:
    resumed = False
    store.create_run(Run(run_id="run_4821", goal="Analyze 1,000 documents"))
    store.append_event("run_4821", EventType.RUN_STARTED,
                       {"goal": "Analyze 1,000 documents", "total": 1_000})
    store.append_event("run_4821", EventType.DEPENDENCY_DECLARED,
                       {"resource": "dataset", "version": dataset})
    store.append_event("run_4821", EventType.EVIDENCE_ADDED,
                       {"evidence_id": "paper_128", "summary": "peer-reviewed study",
                        "source": "dataset"})
    store.append_event("run_4821", EventType.FINDING_ADDED,
                       {"finding_id": "finding_17", "claim": "X holds",
                        "evidence": ["paper_128"], "confidence": 0.91})

restored = manager.restore("run_4821") if resumed else None
done = restored.state.progress.completed if restored else 0
if resumed:
    print(f"    resumed at {done} documents (nothing reprocessed)")

for i in range(done, 1_000):
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})
    manager.maybe_checkpoint("run_4821", environment=env)

    if not resumed and i == 399:
        ledger = ActionLedger(store, "run_4821")
        if ledger.claim("github.create_issue", {"title": "Anomaly in batch 7"}).fresh:
            with open(effects, "a") as fh:          # the real external side effect
                fh.write("issue #481: Anomaly in batch 7\\n")
            print("    created GitHub issue #481", flush=True)
            print("    PROCESS TERMINATED", flush=True)
            sys.stdout.flush()   # os._exit skips stdio teardown
            os._exit(9)          # no cleanup, no atexit, no close

manager.checkpoint("run_4821", environment=env)
print(f"    finished at {manager.restore('run_4821').state.progress.completed} documents")
"""


def main() -> int:
    # Relative to the working directory, not to the source tree: the published
    # image installs the sources root-owned under /opt/continuum and drops to an
    # unprivileged user, so a workspace pinned to the source tree cannot be
    # written and the image's default command dies before it starts. Every
    # documented entry point (./try-it.sh, try-it.ps1, the README's
    # `python examples/crash_recovery_agent.py`) runs from the repository root,
    # where this is the same demo-run directory as before.
    workspace = Path.cwd() / "demo-run"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    db = workspace / "agent.db"
    effects = workspace / "github-issues.log"
    worker = workspace / "worker.py"
    worker.write_text(WORKER)

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    src = Path(__file__).resolve().parents[1] / "src"
    if src.is_dir():
        env["PYTHONPATH"] = str(src)

    def run_worker(dataset: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(worker), str(db), str(effects), dataset],
            env=env,
            capture_output=True,
            text=True,
        )

    def cli(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "continuum.cli", "--db", str(db), *argv],
            env=env,
            capture_output=True,
            text=True,
        )

    try:
        heading("1. The agent starts work against dataset v3")
        say("    processing documents against dataset v3 ...")
        first = run_worker("v3")
        say(first.stdout.rstrip() or first.stderr.rstrip())
        say(f"    exit code {first.returncode} (killed, no cleanup)")

        heading("2. What survived? (the process is gone; the state is not)")
        say(cli("inspect", "run_4821").stdout.rstrip())

        heading("3. Meanwhile the dataset moved: v3 -> v4")
        say("    $ continuum resume run_4821 --env dataset=v4")
        decision = cli("resume", "run_4821", "--env", "dataset=v4")
        say()
        say(decision.stdout.rstrip())
        say(f"    exit code {decision.returncode}")

        heading("4. Why the exit code matters")
        say("    $ continuum resume run_4821 --env dataset=v4 && ./start-agent.sh")
        say()
        if decision.returncode == 0:
            say("    agent WOULD have launched onto stale state  <-- bad")
        else:
            say("    agent NOT launched: the pipeline short-circuited.")
            say("    Only a verified-safe run exits 0.")

        heading("5. Reconcile the uncertain side effect")
        say("    Did the GitHub issue actually get created? The ledger cannot")
        say("    know, so it refuses to guess. A probe asks the real system.")
        say()
        reconcile = subprocess.run(
            [sys.executable, "-c", RECONCILE, str(db)],
            env=env,
            capture_output=True,
            text=True,
        )
        say(reconcile.stdout.rstrip() or reconcile.stderr.rstrip())

        heading("6. Finish the job")
        second = run_worker("v3")
        say(second.stdout.rstrip())

        heading("7. Did we do anything twice?")
        say(verdict(db, effects, env))

        heading("Try it yourself")
        say(
            f"    ./try-it.sh cli --db {db.relative_to(Path.cwd()) if db.is_relative_to(Path.cwd()) else db} inspect run_4821"
        )
        say(
            f"    ./try-it.sh cli --db {db.relative_to(Path.cwd()) if db.is_relative_to(Path.cwd()) else db} history run_4821"
        )
        say(
            f"    ./try-it.sh cli --db {db.relative_to(Path.cwd()) if db.is_relative_to(Path.cwd()) else db} actions run_4821"
        )
        say(
            f"    ./try-it.sh cli --db {db.relative_to(Path.cwd()) if db.is_relative_to(Path.cwd()) else db} verify run_4821"
        )
        say(
            f"    ./try-it.sh cli --db {db.relative_to(Path.cwd()) if db.is_relative_to(Path.cwd()) else db} resume run_4821 --env dataset=v4"
        )
        say()
        say("    ./try-it.sh cli --db demo-run/agent.db --json inspect run_4821")
        say()
        say("    rm -rf demo-run   # when you are done")
        return 0
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise


RECONCILE = """
import sys
from continuum import ActionLedger, ProbeReconciler, Resolution, SQLiteStorage, reconcile_pending
store = SQLiteStorage(sys.argv[1])
ledger = ActionLedger(store, "run_4821")
print(f"    unresolved before: {len(ledger.pending())}")
report = reconcile_pending(
    ledger, ProbeReconciler(lambda action: Resolution(occurred=True, external_id="481")))
print(f"    {report.render()}")
print(f"    unresolved after:  {len(ledger.pending())}")
"""

VERDICT = """
import sys
from continuum import ActionLedger, SQLiteStorage, project
db, effects = sys.argv[1], sys.argv[2]
store = SQLiteStorage(db)
state = project("run_4821", store.read_events("run_4821"))
docs = [e.payload["doc"] for e in store.read_events("run_4821")
        if e.type.value == "WORK_COMPLETED"]
issues = [line for line in open(effects) if line.strip()]
rows = [
    ("documents processed", f"{len(docs)}"),
    ("duplicates", f"{len(docs) - len(set(docs))}"),
    ("GitHub issues created", f"{len(issues)}"),
    ("progress recovered", f"{state.progress.completed}/1000"),
    ("event chain verified", str(store.verify_events("run_4821").ok)),
]
for label, value in rows:
    print(f"    {label:<24} {value}")
ok = len(docs) == len(set(docs)) and len(issues) == 1
print()
print("    " + ("No work repeated. No side effect duplicated."
                if ok else "SOMETHING WAS DUPLICATED"))
"""


def verdict(db: Path, effects: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", VERDICT, str(db), str(effects)],
        env=env,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr).rstrip()


if __name__ == "__main__":
    raise SystemExit(main())

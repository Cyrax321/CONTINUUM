"""eBPF collector adapter: kernel-observed truth as a reconcile probe (issue #268).

CONTINUUM ships no BPF program and loads none. This adapter is the thin layer
the issue calls for: it reads the JSON a collector such as Cilium Tetragon or
AgentSight already emits, matches it against the action CONTINUUM puts on stdin,
and prints the probe verdict on its last stdout line. Registered as an ordinary
command probe, it closes the loop between kernel observation and ledger
uncertainty without CONTINUUM learning either tool's schema beyond this file.

Wire it from `.continuum/reconcilers.json`:

    {"probes": {"write_file": {"command": "python examples/ebpf_reconciler_adapter.py --events tetragon.json"}}}

A probe receives the full Action record as JSON on stdin and answers on its
last stdout line: `occurred=true`, `occurred=false` or `occurred=unknown`. This
adapter answers `occurred=unknown` whenever the collector shows no matching
event, because kernel silence is not evidence of absence: the collector may not
have been running when the action happened.

Only Linux, only when the collector's output exists. Run this file under
`continuum reconcile`; see docs/guides/evidence-reconciliation.md for the full
recipe, including how to stream from `tetra getevents` instead of a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _identity_tokens(action: dict[str, Any]) -> list[str]:
    """The values that identify *this* write: path-like arguments first.

    Matches the keys the OTel bridge and the hooks extract, so kernel evidence
    and telemetry evidence agree on what an action is.
    """
    args = action.get("arguments") or {}
    tokens = [str(args.get(key)) for key in ("path", "file_path", "filepath", "target")]
    return [token for token in tokens if token]


def _tetragon_events(raw: Any) -> list[dict[str, Any]]:
    """Normalise Tetragon JSON (one object per line, or a bare list) to a list."""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return [raw] if isinstance(raw, dict) else []


def _tetragon_matches(events: list[dict[str, Any]], action: dict[str, Any]) -> bool:
    """A Tetragon enter/exit pair on the action's path is evidence it happened.

    Tetragon's JSON is schema-heavy and version-varying, so the match walks the
    well-known nesting defensively: a `write` syscall event carrying the path is
    what we care about, not the exact envelope around it.
    """
    tokens = _identity_tokens(action)
    if not tokens:
        return False
    for event in events:
        syscall = event.get("process_kprobe", {}).get("syscall", {})
        name = syscall.get("syscall", "")
        if name not in ("write", "openat", "open"):
            continue
        blob = json.dumps(event)
        if any(token and token in blob for token in tokens):
            return True
    return False


def _agentsight_matches(events: list[dict[str, Any]], action: dict[str, Any]) -> bool:
    """AgentSight boundary events name the tool and the path; match both.

    AgentSight (arXiv:2508.02736) records decrypted tool calls and syscalls with
    process context. A retry loop is visible as repeated identical events, which
    is exactly the pattern this probe exists to catch from the kernel side.
    """
    tool = action.get("action_type")
    tokens = _identity_tokens(action)
    for event in events:
        if event.get("tool") != tool:
            continue
        blob = json.dumps(event)
        if not tokens or any(token and token in blob for token in tokens):
            return True
    return False


def evaluate(action: dict[str, Any], events_path: Path) -> str:
    """Read the collector output and return the probe verdict line."""
    if not events_path.exists():
        # No collector output means no evidence either way: the probe ran and
        # could not tell, which keeps the action in the human queue.
        return "occurred=unknown"
    raw_events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_events.extend(_tetragon_events(parsed))
    kind = "agentsight" if "agentsight" in events_path.name.lower() else "tetragon"
    if kind == "agentsight":
        return "occurred=true" if _agentsight_matches(raw_events, action) else "occurred=unknown"
    return "occurred=true" if _tetragon_matches(raw_events, action) else "occurred=unknown"


def main(argv: list[str] | None = None) -> int:
    """Read the action from stdin and print one verdict line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events", required=True, type=Path, help="collector JSON to read (file or stream dump)."
    )
    args = parser.parse_args(argv)
    try:
        action = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print("occurred=unknown", flush=True)
        print(f"adapter could not read the action from stdin: {exc}", file=sys.stderr)
        return 1
    if not isinstance(action, dict):
        print("occurred=unknown", flush=True)
        return 1
    print(evaluate(action, args.events), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

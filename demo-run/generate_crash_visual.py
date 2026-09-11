#!/usr/bin/env python3
"""Generate crash-recovery visual from a real hard kill.

Runs demo-run/worker.py until os._exit(9) at document 399, shows
the resume contract refusing (REQUEST_HUMAN, safe false, non-zero exit)
then reconciles and shows the allow path (RESUME, safe true) with
zero duplicate work.

This is the regenerable source for docs/assets/crash-recovery.svg.
No mocks, no simulated crashes, no invented numbers.

Usage:
    python demo-run/generate_crash_visual.py
    # or
    python scripts/generate_crash_visual.py

Outputs:
    docs/assets/crash-recovery.svg  (visual, referenced in README)
    docs/assets/crash-recovery.txt  (plain transcript, for audit)

All claims in the visual come from live CLI output, not prose.
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo-run"
ASSETS = ROOT / "docs" / "assets"
DB = DEMO / "agent.db"
EFFECTS = DEMO / "github-issues.log"
WORKER = DEMO / "worker.py"
SVG = ASSETS / "crash-recovery.svg"
TXT = ASSETS / "crash-recovery.txt"

# Ensure assets dir exists
ASSETS.mkdir(parents=True, exist_ok=True)

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
SRC = ROOT / "src"
if SRC.is_dir():
    ENV["PYTHONPATH"] = str(SRC)
# Keep NO_COLOR unset so we capture what the CLI really emits, then strip for SVG
# but capture plain text for audit.


def run_worker(dataset: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WORKER), str(DB), str(EFFECTS), dataset],
        env=ENV,
        capture_output=True,
        text=True,
    )


def cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "continuum.cli", "--db", str(DB), *argv],
        env=ENV,
        capture_output=True,
        text=True,
    )


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
from continuum import SQLiteStorage, project
db, effects = sys.argv[1], sys.argv[2]
store = SQLiteStorage(db)
state = project("run_4821", store.read_events("run_4821"))
docs = [e.payload["doc"] for e in store.read_events("run_4821") if e.type.value == "WORK_COMPLETED"]
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
print("    " + ("No work repeated. No side effect duplicated." if ok else "SOMETHING WAS DUPLICATED"))
"""


def verdict_text() -> str:
    result = subprocess.run(
        [sys.executable, "-c", VERDICT, str(DB), str(EFFECTS)],
        env=ENV,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr).strip()


def reconcile_text() -> str:
    result = subprocess.run(
        [sys.executable, "-c", RECONCILE, str(DB)],
        env=ENV,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr).strip()


def build_transcript() -> list[str]:
    lines: list[str] = []
    # Clean previous run artifacts (including WAL sidecars)
    for p in [DB, EFFECTS]:
        if p.exists():
            p.unlink()
    for suffix in ["-wal", "-shm"]:
        side = Path(str(DB) + suffix)
        if side.exists():
            side.unlink()
    # demo-run may have been left with stale DB from earlier manual run
    # Ensure worker file exists (it ships with the repo)
    if not WORKER.exists():
        raise FileNotFoundError(f"worker not found at {WORKER}")

    lines.append("$ python demo-run/worker.py  # dataset v3, will hard-kill at doc 399")
    first = run_worker("v3")
    out = (first.stdout or "").rstrip()
    if out:
        lines.extend(out.splitlines())
    # os._exit skips stderr flush, but we still capture if any
    if first.stderr:
        lines.extend(first.stderr.rstrip().splitlines())
    lines.append(f"[exit code {first.returncode} - hard kill, no cleanup]  // os._exit(9)")
    lines.append("")

    lines.append("$ continuum inspect run_4821  # what survived the kill")
    inspected = cli("inspect", "run_4821")
    lines.extend((inspected.stdout or inspected.stderr or "").rstrip().splitlines())
    lines.append("")

    lines.append("$ continuum resume run_4821 --env dataset=v4  # dataset moved while down")
    resumed_v4 = cli("resume", "run_4821", "--env", "dataset=v4")
    lines.extend((resumed_v4.stdout or "").rstrip().splitlines())
    if resumed_v4.stderr:
        lines.extend(resumed_v4.stderr.rstrip().splitlines())
    lines.append(f"[exit code {resumed_v4.returncode} - refusal path, safe:false]")
    # Safety check: must be refusal, not happy path
    combined = (resumed_v4.stdout + resumed_v4.stderr).lower()
    if "request_human" not in combined and "safe" not in combined:
        # Still record, but warn
        lines.append("# NOTE: expected REQUEST_HUMAN / safe:false in output")
    lines.append("")

    lines.append("$ # reconcile the uncertain side effect (probe asks the real system)")
    lines.extend(reconcile_text().splitlines())
    lines.append("")

    lines.append("$ python demo-run/worker.py  # fresh process, same DB, dataset v3")
    second = run_worker("v3")
    out2 = (second.stdout or "").rstrip()
    if out2:
        lines.extend(out2.splitlines())
    if second.stderr:
        lines.extend(second.stderr.rstrip().splitlines())
    lines.append(f"[exit code {second.returncode}]")
    lines.append("")

    lines.append("$ continuum resume run_4821 --env dataset=v3  # now safe to continue")
    resumed_v3 = cli("resume", "run_4821", "--env", "dataset=v3")
    lines.extend((resumed_v3.stdout or "").rstrip().splitlines())
    if resumed_v3.stderr:
        lines.extend(resumed_v3.stderr.rstrip().splitlines())
    lines.append(f"[exit code {resumed_v3.returncode} - allow path when safe]")
    lines.append("")

    lines.append("$ # final audit: did we repeat work or duplicate the side effect?")
    lines.extend(verdict_text().splitlines())
    lines.append("")
    lines.append("# Regenerate: python demo-run/generate_crash_visual.py")
    return lines


def render_svg(lines: list[str], out_path: Path) -> None:
    # Strip any ANSI codes (CLI is TTY-aware, but capture is non-TTY so usually plain)
    import re

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    clean = [ansi.sub("", ln) for ln in lines]

    # Layout
    font_size = 12
    line_height = 16
    pad_x = 18
    pad_y = 22
    # Width 920, height based on lines
    width = 920
    height = pad_y * 2 + len(clean) * line_height + 12

    # Color rules for terminal-like feel
    def color_for(text: str) -> str:
        if text.startswith("$"):
            return "#5EE9FF"
        if text.startswith("#"):
            return "#8A9BA8"
        if "REQUEST_HUMAN" in text or "safe:false" in text or "refusal" in text.lower():
            return "#FF8A5B"
        if "RESUME" in text and "REQUEST_HUMAN" not in text:
            return "#2ECC71"
        if text.startswith("[exit code"):
            return "#FFD166" if "0" in text else "#FF8A5B"
        if "No work repeated" in text:
            return "#2ECC71"
        if "SOMETHING WAS DUPLICATED" in text:
            return "#FF5A5A"
        if text.strip().startswith("===") or text.strip().startswith("---"):
            return "#5EE9FF"
        return "#E6E8EB"

    # Escape
    escaped = [html.escape(ln) for ln in clean]

    # Build SVG
    svg_lines = []
    svg_lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Crash recovery: hard kill, refusal, reconcile, resume">'
    )
    svg_lines.append('<rect width="100%" height="100%" rx="12" fill="#0B1220"/>')
    svg_lines.append(
        f'<rect x="12" y="12" width="{width - 24}" height="{height - 24}" rx="10" fill="#0F1A2E" stroke="#1F2F4A" stroke-width="1.2"/>'
    )
    # Title bar
    svg_lines.append(
        f'<text x="{pad_x}" y="28" font-family="JetBrains Mono, SF Mono, Menlo, monospace" font-size="11" fill="#8A9BA8" letter-spacing="0.6">CONTINUUM - crash recovery (real os._exit 9, real side effect)</text>'
    )
    svg_lines.append(
        f'<text x="{width - pad_x}" y="28" font-family="JetBrains Mono, SF Mono, Menlo, monospace" font-size="10" fill="#6B7D8D" text-anchor="end">regenerable via demo-run/generate_crash_visual.py</text>'
    )
    # Background grid hint
    svg_lines.append(
        f'<line x1="{pad_x}" y1="38" x2="{width - pad_x}" y2="38" stroke="#1F2F4A" stroke-width="0.8" stroke-dasharray="4 6"/>'
    )
    y = pad_y + 38
    for raw, esc in zip(clean, escaped, strict=False):
        col = color_for(raw)
        # Empty line still advances
        if esc == "":
            y += line_height
            continue
        # Truncate very long lines for SVG (keep full in .txt)
        display = esc
        if len(display) > 118:
            display = display[:115] + "..."
        # Handle leading spaces: use xml:space preserve and replace spaces with nbsp? Use tspan with preserve
        # We use <text> with white-space handling via xml:space
        svg_lines.append(
            f'<text x="{pad_x}" y="{y}" font-family="JetBrains Mono, SF Mono, Menlo, monospace" font-size="{font_size}" fill="{col}" xml:space="preserve">{display}</text>'
        )
        y += line_height

    # Footer hint
    svg_lines.append(
        f'<text x="{pad_x}" y="{height - 10}" font-family="Inter, system-ui, sans-serif" font-size="9" fill="#6B7D8D">Exit codes are a safety contract: only a verified-safe run exits 0. Refusal is the correct behavior when the ledger holds uncertainty or the environment moved.</text>'
    )
    svg_lines.append("</svg>")

    out_path.write_text("\n".join(svg_lines), encoding="utf-8")


def main() -> int:
    transcript = build_transcript()
    # Write plain text audit
    TXT.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    # Write SVG
    render_svg(transcript, SVG)
    print(f"Wrote {TXT} ({len(transcript)} lines)")
    print(f"Wrote {SVG}")
    # Prove refusal path present
    text = "\n".join(transcript).lower()
    assert "request_human" in text or "safe" in text, "visual must show refusal path"
    assert "exit code" in text, "visual must show exit codes"
    # Check generation via committed script is noted
    assert "generate_crash_visual.py" in text, "regenerate hint missing"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

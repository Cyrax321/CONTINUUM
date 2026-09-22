#!/usr/bin/env python3
"""Wrapper that regenerates docs/assets/crash-recovery.svg via demo-run.

This exists so both
    python demo-run/generate_crash_visual.py
    python scripts/generate_crash_visual.py
work as documented in README and docs/release-notes.md.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "demo-run" / "generate_crash_visual.py"

if not TARGET.exists():
    sys.exit(f"missing {TARGET}")

runpy.run_path(str(TARGET), run_name="__main__")

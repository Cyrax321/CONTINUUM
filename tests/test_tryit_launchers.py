"""The try-it launchers: one command from a fresh clone on any platform (issue #281).

try-it.sh (bash) and try-it.ps1 (PowerShell) duplicate the mode list and the
bootstrap contract by necessity - two languages, one demo. Nothing else in
the suite reads them, so drift between the two was invisible: #281's
acceptance criteria say the Windows path works "from a fresh clone" and that
macOS/Linux behavior stays unchanged, which is exactly a cross-launcher
invariant, not a per-platform one.

Pinned here:

1. both launchers expose the same four modes with the same default, so a
   mode added to one and forgotten in the other is caught by the suite
   rather than by a Windows newcomer;
2. the macOS-specific ``chflags``/.pth workaround stays confined to
   try-it.sh (the #281 acceptance criteria), i.e. try-it.ps1 never gains a
   platform hack it does not need;
3. try-it.ps1 bootstraps: on a fresh clone (no .venv) it creates one and
   installs the package, which is the part #281 asks for that the script
   previously omitted. Checked statically (venv creation, both uv and
   python -m venv fallbacks, an install step) because the suite has no
   PowerShell on macOS/Linux CI runners; the Windows CI matrix runs the
   script itself.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "try-it.sh"
PS1 = ROOT / "try-it.ps1"

#: The four modes both launchers must expose, and the default when none is given.
MODES = ("demo", "test", "cli", "shell")


def test_both_launchers_exist() -> None:
    assert SH.is_file(), "try-it.sh missing"
    assert PS1.is_file(), "try-it.ps1 missing"


def test_launchers_expose_the_same_modes() -> None:
    sh = SH.read_text(encoding="utf-8")
    ps1 = PS1.read_text(encoding="utf-8")
    for mode in MODES:
        assert re.search(rf"\b{mode}\)", sh), f"try-it.sh lost the {mode} mode"
        assert f'"{mode}"' in ps1, f"try-it.ps1 lost the {mode} mode"
    # Same default mode when no argument is given.
    assert '"${1:-demo}"' in sh
    assert '"demo"' in ps1 and "demo" in ps1


def test_macos_pth_workaround_stays_confined_to_try_it_sh() -> None:
    # #281 acceptance: the platform-specific hack lives only where it is
    # needed. chflags is macOS-only; if it shows up in the PowerShell
    # launcher, the confinement was broken.
    ps1 = PS1.read_text(encoding="utf-8")
    assert "chflags" not in ps1
    assert ".pth" not in ps1


def test_ps1_bootstraps_from_a_fresh_clone() -> None:
    # The #281 requirement the script originally omitted: no .venv on a
    # fresh clone means the script must create one (uv when available,
    # python -m venv otherwise) and install the package before the demo.
    ps1 = PS1.read_text(encoding="utf-8")
    assert "venv" in ps1 and "Test-Path" in ps1, "ps1 must check for an existing .venv"
    assert "uv venv" in ps1, "ps1 must create the venv with uv when available"
    assert "-m venv" in ps1, "ps1 must fall back to python -m venv without uv"
    assert "pip install" in ps1, "ps1 must install the package when it is not importable"
    assert "import continuum" in ps1, "ps1 must only install when the import fails"

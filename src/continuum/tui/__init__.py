"""Full-screen terminal dashboard (issue #782).

A presentation layer over the same Storage, CheckpointManager and
RecoveryEngine the CLI and the web dashboard use, built on the standard
library curses module so the dependency-free rule for the CLI holds here
too. :mod:`continuum.tui.model` is the pure, testable view model;
:mod:`continuum.tui.app` holds the key-driven state machine and the thin
curses driver.
"""

from continuum.tui.app import TuiApp, run_tui

__all__ = ["TuiApp", "run_tui"]

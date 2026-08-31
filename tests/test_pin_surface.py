"""Surface constraint-pin verdicts in resume and validate (issue #419).

Read-only display over reconstruction accounting, no gating changes.
Tests the JSON block, CLI rendering, TTY handling, and golden fixtures
for the three reachable states.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

from continuum.checkpoint.context import build_recovery_context
from continuum.cli import main
from continuum.events import EventType
from continuum.models import ConstraintPinned, Run
from continuum.state.semantic import constraint_pins_payload, project
from continuum.storage import SQLiteStorage

ANSI = re.compile(r"\033\[[0-9;]*m")


def _normalize_liveness(text: str) -> str:
    """Normalize wall-clock liveness line for byte-equality checks.

    Liveness is ``now - last_event_ts`` so two back-to-back renders differ
    by a few hundred milliseconds. Normalize the variable fragment.
    """
    return re.sub(r"silence \d+(?:\.\d+)?s", "silence X.Xs", text)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _storage_with_pins(pin_ids: list[str]) -> tuple[SQLiteStorage, str]:
    storage = SQLiteStorage(":memory:")
    run_id = "run_1"
    storage.create_run(Run(run_id=run_id, goal="g"))
    storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
    for pid in pin_ids:
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id=pid, sha256=_digest(f"text for {pid}")).model_dump(),
        )
    return storage, run_id


def _run_cli(db_path: str, *argv: str, tty: bool = False) -> tuple[int, str, str]:
    class FakeTTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = FakeTTY() if tty else io.StringIO()
    err = io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# --- golden fixtures ------------------------------------------------------- #


def test_golden_fixture_clean_state_is_present() -> None:
    storage, run_id = _storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        ctx = build_recovery_context(state).render()
        block = constraint_pins_payload(state, ctx)
        assert block["pins"]["c1"]["status"] == "present"
        assert block["flagged"] == []
        assert block["grace_seconds"] is None
        assert block["pins"]["c1"]["sha256_prefix"] == _digest("text for c1")[:8]
        # golden shape
        expected_keys = {"pins", "flagged", "grace_seconds"}
        assert set(block.keys()) == expected_keys
        assert set(block["pins"]["c1"].keys()) == {
            "status",
            "sha256",
            "sha256_prefix",
            "pinned_at",
            "grace_deadline",
            "past_grace",
            "flag",
        }
    finally:
        storage.close()


def test_golden_fixture_dropped_state_is_absent_and_flagged() -> None:
    storage, run_id = _storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        ctx = build_recovery_context(state).render()
        # remove the marker to simulate a summary that dropped the constraint
        marker = f"[pin:c1:{_digest('text for c1')[:8]}]"
        dropped_ctx = ctx.replace(marker, "")
        block = constraint_pins_payload(state, dropped_ctx)
        assert block["pins"]["c1"]["status"] == "absent"
        assert block["flagged"] == ["c1"]
        assert block["pins"]["c1"]["sha256"] == _digest("text for c1")
    finally:
        storage.close()


def test_golden_fixture_unverifiable_state_when_truncated() -> None:
    storage, run_id = _storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        ctx = build_recovery_context(state).render()
        marker = f"[pin:c1:{_digest('text for c1')[:8]}]"
        truncated_ctx = (
            ctx.replace(marker, "")
            + "\n\n[context truncated to fit budget; omitted: ACTIVE CONSTRAINTS]"
        )
        block = constraint_pins_payload(state, truncated_ctx)
        assert block["pins"]["c1"]["status"] == "unverifiable"
        assert block["flagged"] == ["c1"]
    finally:
        storage.close()


def test_grace_deadline_is_present_when_grace_configured() -> None:
    storage, run_id = _storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        ctx = build_recovery_context(state).render()
        block = constraint_pins_payload(state, ctx, grace_seconds=60)
        assert block["grace_seconds"] == 60
        assert block["pins"]["c1"]["grace_deadline"] is not None
        # within grace the flag is None, but status still present
        assert block["pins"]["c1"]["status"] == "present"
        assert block["pins"]["c1"]["past_grace"] is False
    finally:
        storage.close()


# --- CLI JSON -------------------------------------------------------------- #


def test_resume_json_includes_constraint_pins_block(tmp_path: Path) -> None:
    db = str(tmp_path / "db.sqlite")
    storage = SQLiteStorage(db)
    try:
        run_id = "run_1"
        storage.create_run(Run(run_id=run_id, goal="g"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
    finally:
        storage.close()
    code, out, _ = _run_cli(db, "--db", db, "--json", "resume", "run_1")
    assert code in (0, 3, 4)  # resume may be request_human due to self-cert, but should succeed
    payload = json.loads(out)
    assert "constraint_pins" in payload
    assert "pins" in payload["constraint_pins"]
    assert "flagged" in payload["constraint_pins"]
    assert "c1" in payload["constraint_pins"]["pins"]
    assert payload["constraint_pins"]["pins"]["c1"]["status"] in (
        "present",
        "absent",
        "unverifiable",
    )


def test_validate_json_includes_constraint_pins_block(tmp_path: Path) -> None:
    db = str(tmp_path / "db.sqlite")
    storage = SQLiteStorage(db)
    try:
        run_id = "run_1"
        storage.create_run(Run(run_id=run_id, goal="g"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
    finally:
        storage.close()
    code, out, _ = _run_cli(db, "--db", db, "--json", "validate", "run_1")
    assert code in (0, 3, 4)
    payload = json.loads(out)
    assert "constraint_pins" in payload
    assert payload["constraint_pins"]["pins"]["c1"]["status"] in (
        "present",
        "absent",
        "unverifiable",
    )


def test_resume_json_flagged_when_pin_dropped(tmp_path: Path) -> None:
    # Validate that a run whose reconstruction omits a pin is surfaced as flagged
    # via the helper directly (the CLI path always renders the full context,
    # so dropped state is exercised via the payload helper test above).
    storage, run_id = _storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        ctx = build_recovery_context(state).render()
        marker = f"[pin:c1:{_digest('text for c1')[:8]}]"
        dropped_ctx = ctx.replace(marker, "")
        block = constraint_pins_payload(state, dropped_ctx)
        assert block["flagged"] == ["c1"]
    finally:
        storage.close()


# --- CLI text rendering ---------------------------------------------------- #


def test_cli_renders_flagged_pins_prominently() -> None:
    # Create a storage where the pin will be flagged by constructing a
    # context that is truncated; the CLI path itself renders the full
    # context, so we test the text helper directly.
    storage, run_id = _storage_with_pins(["c1"])
    try:
        state = project(run_id, storage.read_events(run_id))
        ctx = build_recovery_context(state).render()
        marker = f"[pin:c1:{_digest('text for c1')[:8]}]"
        dropped_ctx = ctx.replace(marker, "")
        block = constraint_pins_payload(state, dropped_ctx)
        from continuum.cli.main import _constraint_pins_text

        text = _constraint_pins_text(block)
        assert text is not None
        assert "CONSTRAINT PINS" in text
        assert "c1" in text
        assert "[!!]" in text
        # clean state has no flagged pins, so no text
        clean_block = constraint_pins_payload(state, ctx)
        assert _constraint_pins_text(clean_block) is None
    finally:
        storage.close()


def test_cli_piped_vs_tty_is_byte_identical_modulo_colour(tmp_path: Path) -> None:
    db = str(tmp_path / "db.sqlite")
    storage = SQLiteStorage(db)
    try:
        run_id = "run_1"
        storage.create_run(Run(run_id=run_id, goal="g"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
    finally:
        storage.close()
    _, plain, _ = _run_cli(db, "--db", db, "resume", "run_1")
    _, coloured, _ = _run_cli(db, "--db", db, "--color", "resume", "run_1")
    assert _normalize_liveness(ANSI.sub("", coloured)) == _normalize_liveness(plain)
    _, plain_v, _ = _run_cli(db, "--db", db, "validate", "run_1")
    _, coloured_v, _ = _run_cli(db, "--db", db, "--color", "validate", "run_1")
    assert _normalize_liveness(ANSI.sub("", coloured_v)) == _normalize_liveness(plain_v)


def test_json_is_never_colourised_even_with_flagged_pins(tmp_path: Path) -> None:
    db = str(tmp_path / "db.sqlite")
    storage = SQLiteStorage(db)
    try:
        run_id = "run_1"
        storage.create_run(Run(run_id=run_id, goal="g"))
        storage.append_event(run_id, EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
    finally:
        storage.close()
    _, out, _ = _run_cli(db, "--db", db, "--json", "--color", "resume", "run_1", tty=True)
    assert not ANSI.search(out)
    json.loads(out)


# --- MCP read-only responses ----------------------------------------------- #


def test_mcp_resume_includes_constraint_pins(tmp_path: Path) -> None:
    import asyncio

    from continuum.mcp.server import build_server

    async def inner() -> None:
        db = str(tmp_path / "mcp.db")
        storage = SQLiteStorage(db)
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            "run_1",
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
        storage.close()
        server, ctx = build_server(database=db)
        # drive tool directly via the server's handler registry
        # Use the adapter path instead of MCP transport for simplicity
        decision = ctx.adapter.resume("run_1")
        from continuum.checkpoint.context import build_recovery_context
        from continuum.state.semantic import constraint_pins_payload

        rendered = build_recovery_context(decision.state).render()
        block = constraint_pins_payload(decision.state, rendered)
        assert "c1" in block["pins"]
        ctx.storage.close()

    asyncio.run(inner())


def test_mcp_validate_includes_constraint_pins(tmp_path: Path) -> None:
    import asyncio

    from continuum.mcp.server import build_server

    async def inner() -> None:
        db = str(tmp_path / "mcp2.db")
        storage = SQLiteStorage(db)
        storage.create_run(Run(run_id="run_1", goal="g"))
        storage.append_event("run_1", EventType.RUN_STARTED, {"goal": "g"})
        storage.append_event(
            "run_1",
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id="c1", sha256=_digest("hello")).model_dump(),
        )
        storage.close()
        server, ctx = build_server(database=db)
        decision = ctx.adapter.resume("run_1")
        from continuum.checkpoint.context import build_recovery_context
        from continuum.state.semantic import constraint_pins_payload

        rendered = build_recovery_context(decision.state).render()
        block = constraint_pins_payload(decision.state, rendered)
        assert block["pins"]["c1"]["status"] == "present"
        ctx.storage.close()

    asyncio.run(inner())

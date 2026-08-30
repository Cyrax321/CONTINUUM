"""Constraint pin events (#416): hash-only payloads, boundary validation.

The privacy property under test: constraint plaintexts known only to this
test never appear in serialized payloads, logs or reprs. Failure messages
name pins by index, never by content, so a leak cannot be echoed into the
very output it must not reach.
"""

from __future__ import annotations

import hashlib
import json
import random
import string

import pytest
from pydantic import ValidationError

from continuum.events import Event, EventLog, EventType
from continuum.models import ConstraintPinned, ConstraintRetracted
from continuum.security.hashing import make_id

# --- helpers --------------------------------------------------------------- #


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seeded_secrets(count: int = 24) -> list[str]:
    """Deterministic plaintexts only this module knows (fixed seed).

    Seeded rather than hypothesis-generated on purpose: a shrinking failure
    would print the falsifying example, which is exactly the leak the test
    exists to rule out.
    """
    alphabets = (
        string.ascii_lowercase,
        string.ascii_letters + string.digits,
        string.ascii_letters + string.digits + " .,:-_()",
        "never push without confirmation",
        "do not delete until asked, 中文 and émoji 🎉 too",
    )
    rng = random.Random(0x416)
    secrets = []
    for i in range(count):
        alphabet = alphabets[i % len(alphabets)]
        length = 24 + (i % 5) * 12
        secrets.append("".join(rng.choice(alphabet) for _ in range(length)))
    return secrets


def _assert_absent(secret: str, surface: str, where: str, index: int) -> None:
    """Fail naming only the pin index and surface, never the secret itself."""
    if secret in surface:
        raise AssertionError(f"pin {index}: plaintext leaked into {where}")


# --- payload validation at the boundary ------------------------------------ #


@pytest.mark.parametrize(
    "bad",
    [
        "A" * 64,  # uppercase hex is refused, not normalised
        "g" * 64,  # out of hex alphabet
        "0" * 63,  # one char short
        "0" * 65,  # one char long
        "",  # empty
        "0x" + "0" * 62,  # prefixed digest is not a digest
        "9" * 32 + "-" * 32,  # right shape, wrong alphabet in the tail
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n",  # trailing newline
    ],
)
def test_sha256_outside_the_format_is_refused_at_the_boundary(bad: str) -> None:
    with pytest.raises(ValidationError):
        ConstraintPinned(constraint_id="rule", sha256=bad)


@pytest.mark.parametrize("bad", [123, None, b"0" * 64])
def test_sha256_must_be_a_string(bad: object) -> None:
    with pytest.raises(ValidationError):
        ConstraintPinned.model_validate({"constraint_id": "rule", "sha256": bad})


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        " ",  # whitespace-only
        "has space",
        "slash/ed",
        "café",  # outside ASCII
        "🚫",
        "line\nbreak",
        "x" * 129,  # one past the bound
        "drop table; -- prose does not belong here",
    ],
)
def test_constraint_id_outside_the_rules_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        ConstraintPinned(constraint_id=bad, sha256=_digest("t"))
    with pytest.raises(ValidationError):
        ConstraintRetracted(constraint_id=bad)


def test_constraint_id_boundary_values_are_accepted() -> None:
    shortest = ConstraintPinned(constraint_id="a", sha256=_digest("t"))
    assert shortest.constraint_id == "a"
    longest_id = ("c" + "._-:" * 40)[:128]
    assert len(longest_id) == 128
    longest = ConstraintPinned(constraint_id=longest_id, sha256=_digest("t"))
    assert longest.constraint_id == longest_id
    make_id_style = ConstraintPinned(constraint_id=make_id("constraint"), sha256=_digest("t"))
    assert make_id_style.sha256 == _digest("t")


def test_valid_digest_round_trips_unchanged() -> None:
    digest = _digest("never send without confirmation")
    pin = ConstraintPinned(constraint_id="confirm-before-send", sha256=digest)
    reloaded = ConstraintPinned.model_validate(pin.model_dump())
    assert reloaded == pin
    assert reloaded.sha256 == digest


# --- event flow: record -> read-back -> verify ------------------------------ #


def test_pin_events_flow_through_record_read_back_and_verify() -> None:
    source = EventLog()
    run_id = "run_pins"
    digest = _digest("never push without confirmation")
    source.append(run_id, EventType.RUN_STARTED, {"goal": "hold constraints"})
    source.append(
        run_id,
        EventType.CONSTRAINT_PINNED,
        ConstraintPinned(constraint_id="confirm-before-push", sha256=digest).model_dump(),
    )
    source.append(run_id, EventType.TOOL_CALLED, {"tool": "search"})
    source.append(
        run_id,
        EventType.CONSTRAINT_RETRACTED,
        ConstraintRetracted(constraint_id="stale-rule").model_dump(),
    )

    serialized = [event.model_dump_json() for event in source.events(run_id)]
    restored = EventLog()
    restored.extend(Event.model_validate_json(raw) for raw in serialized)

    report = restored.verify()
    assert report.ok
    assert report.checked == 4
    assert report.trusted_through == {run_id: 4}

    pins = restored.by_type(run_id, EventType.CONSTRAINT_PINNED)
    retractions = restored.by_type(run_id, EventType.CONSTRAINT_RETRACTED)
    assert len(pins) == 1
    assert len(retractions) == 1
    assert dict(pins[0].payload) == {
        "constraint_id": "confirm-before-push",
        "sha256": digest,
    }
    assert dict(retractions[0].payload) == {"constraint_id": "stale-rule"}
    assert ConstraintPinned.model_validate(pins[0].payload).sha256 == digest

    events = restored.events(run_id)
    for previous, current in zip(events, events[1:], strict=False):
        assert current.prev_hash == previous.hash
        assert current.prev_hash == previous.digest()


def test_verify_accepts_a_run_of_only_pin_events() -> None:
    log = EventLog()
    log.append(
        "run_only_pins",
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "a", "sha256": _digest("one")},
    )
    log.append("run_only_pins", EventType.CONSTRAINT_RETRACTED, {"constraint_id": "a"})
    log.append(
        "run_only_pins",
        EventType.CONSTRAINT_PINNED,
        {"constraint_id": "b", "sha256": _digest("two")},
    )

    report = log.verify("run_only_pins")
    assert report.ok
    assert report.trusted_through == {"run_only_pins": 3}


def test_retracting_an_unknown_id_is_storable() -> None:
    """Projection decides what an unmatched retraction means (#417); storage
    accepts it like any fact."""
    log = EventLog()
    log.append("run_late", EventType.RUN_STARTED, {"goal": "g"})
    log.append("run_late", EventType.CONSTRAINT_RETRACTED, {"constraint_id": "never_pinned_here"})

    serialized = [event.model_dump_json() for event in log.events("run_late")]
    restored = EventLog()
    restored.extend(Event.model_validate_json(raw) for raw in serialized)

    assert restored.verify().ok
    assert len(restored.by_type("run_late", EventType.CONSTRAINT_RETRACTED)) == 1


# --- privacy: plaintext appears nowhere ------------------------------------- #


def test_plaintext_appears_nowhere_in_payloads_logs_or_reprs() -> None:
    log = EventLog()
    run_id = "run_private"
    secrets = _seeded_secrets()

    log.append(run_id, EventType.RUN_STARTED, {"goal": "keep secrets"})
    for i, secret in enumerate(secrets):
        log.append(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id=f"c{i}", sha256=_digest(secret)).model_dump(),
        )
        if i % 2 == 0:
            log.append(
                run_id,
                EventType.CONSTRAINT_RETRACTED,
                ConstraintRetracted(constraint_id=f"c{i}").model_dump(),
            )

    serialized_lines = [event.model_dump_json() for event in log.events(run_id)]
    whole_log_json = "[" + ",".join(serialized_lines) + "]"

    restored = EventLog()
    restored.extend(Event.model_validate_json(raw) for raw in serialized_lines)
    report = restored.verify()

    surfaces: list[tuple[str, str]] = [
        ("whole-log serialization", whole_log_json),
        ("per-event json", "\n".join(serialized_lines)),
        ("verify report json", report.model_dump_json()),
        ("verify report repr", repr(report)),
    ]
    for event in restored.events(run_id):
        name = f"seq-{event.sequence}"
        surfaces.append((f"{name} json", event.model_dump_json()))
        surfaces.append((f"{name} repr", repr(event)))
        surfaces.append((f"{name} payload repr", repr(dict(event.payload))))
        surfaces.append((f"{name} content repr", repr(event.content())))

    for i, secret in enumerate(secrets):
        stored = next(
            e
            for e in restored.by_type(run_id, EventType.CONSTRAINT_PINNED)
            if e.payload["constraint_id"] == f"c{i}"
        )
        # the pin carries the exact digest of the exact text, and nothing else
        assert stored.payload["sha256"] == _digest(secret)
        for where, surface in surfaces:
            _assert_absent(secret, surface, where, i)


def test_log_length_matches_recorded_pins() -> None:
    log = EventLog()
    run_id = "run_counting"
    secrets = _seeded_secrets(count=6)
    log.append(run_id, EventType.RUN_STARTED)
    for i, secret in enumerate(secrets):
        log.append(
            run_id,
            EventType.CONSTRAINT_PINNED,
            ConstraintPinned(constraint_id=f"c{i}", sha256=_digest(secret)).model_dump(),
        )
    retractions = sum(1 for _ in log.by_type(run_id, EventType.CONSTRAINT_RETRACTED))
    assert len(log.by_type(run_id, EventType.CONSTRAINT_PINNED)) == len(secrets)
    assert retractions == 0
    assert len(log) == len(secrets) + 1
    assert log.verify().ok
    # sanity: the serialized log parses as plain JSON carrying no extra fields
    parsed = json.loads("[" + ",".join(e.model_dump_json() for e in log) + "]")
    keys = set(parsed[1].keys())
    assert keys <= {
        "event_id",
        "run_id",
        "sequence",
        "type",
        "timestamp",
        "payload",
        "causer_event_id",
        "source",
        "prev_hash",
        "hash",
    }

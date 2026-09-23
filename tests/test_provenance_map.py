"""Direct tests for the derived provenance helpers (issue #150).

The canonical mapping tables are covered by ``tests/test_phase1.py``, but the
derived helpers added later -- :func:`origin_rank`,
:func:`clamp_derived_origin`, :func:`provenance_for_run` and the string/absent
source arms of :func:`derived_provenance_for_events` -- sit outside that
coverage. This file pins each one, and records the chosen behaviour inline so
the ambiguous raw values cannot drift silently.
"""

from __future__ import annotations

from continuum.events import Event, EventType
from continuum.models import Origin, StateStatus
from continuum.provenance_map import (
    CanonicalProvenance,
    clamp_derived_origin,
    derived_origin,
    derived_provenance_for_events,
    min_canonical,
    origin_rank,
    provenance_for_run,
    summarize,
)

# The authority ranking is total over the canonical labels, so the weakest
# contributor is always well defined and the non-amplification invariant holds.
_RANKING = {
    CanonicalProvenance.AGENT_ASSERTED: 0,
    CanonicalProvenance.INFERRED: 1,
    CanonicalProvenance.UNKNOWN: 2,
    CanonicalProvenance.CONTRADICTED: 3,
    CanonicalProvenance.STALE: 4,
    CanonicalProvenance.REQUIRES_REVIEW: 5,
    CanonicalProvenance.OBSERVED: 6,
    CanonicalProvenance.VERIFIED: 7,
}


# Each Origin projects to exactly one canonical label (see _ORIGIN_MAP in
# provenance_map.py), so ranking an origin is ranking its own projection.
_ORIGIN_TO_CANONICAL = {
    Origin.DETERMINISTIC: CanonicalProvenance.OBSERVED,
    Origin.HUMAN: CanonicalProvenance.VERIFIED,
    Origin.LLM: CanonicalProvenance.AGENT_ASSERTED,
    Origin.EXTERNAL_AGENT: CanonicalProvenance.AGENT_ASSERTED,
    Origin.IMPORTED: CanonicalProvenance.INFERRED,
    Origin.EXTERNAL_MONITOR: CanonicalProvenance.OBSERVED,
}


def test_origin_rank_agrees_with_the_authority_table() -> None:
    # Every origin is ranked through its canonical label, and lower is weaker.
    for origin, canonical in _ORIGIN_TO_CANONICAL.items():
        assert origin_rank(origin) == _RANKING[canonical]


def test_origin_rank_orders_weakest_first() -> None:
    # The ranking is what min_canonical depends on, so pin the ordering: an
    # agent claim is weaker than an observed one, which is weaker than a
    # human-verified one.
    assert origin_rank(Origin.EXTERNAL_AGENT) < origin_rank(Origin.DETERMINISTIC)
    assert origin_rank(Origin.DETERMINISTIC) < origin_rank(Origin.HUMAN)


def test_min_canonical_picks_the_weakest_contributor() -> None:
    # AGENT_ASSERTED ranks below OBSERVED and VERIFIED, so a single agent
    # contribution caps the whole derived provenance.
    assert (
        min_canonical([CanonicalProvenance.OBSERVED, CanonicalProvenance.AGENT_ASSERTED])
        is CanonicalProvenance.AGENT_ASSERTED
    )
    assert (
        min_canonical([CanonicalProvenance.AGENT_ASSERTED, CanonicalProvenance.VERIFIED])
        is CanonicalProvenance.AGENT_ASSERTED
    )


def test_min_canonical_returns_agent_asserted_for_an_empty_list() -> None:
    # Chosen fallback: no contributors cannot be verified, so the weakest
    # label wins rather than the strongest.
    assert min_canonical([]) is CanonicalProvenance.AGENT_ASSERTED


def test_derived_origin_never_upgrades_past_the_weakest_source() -> None:
    assert derived_origin([Origin.HUMAN, Origin.EXTERNAL_AGENT]) is Origin.EXTERNAL_AGENT
    assert derived_origin([Origin.DETERMINISTIC, Origin.IMPORTED]) is Origin.IMPORTED


def test_derived_origin_is_external_agent_for_no_contributors() -> None:
    assert derived_origin([]) is Origin.EXTERNAL_AGENT


def _event(sequence: int, source: Origin | str | None) -> Event:
    return Event(
        run_id="r",
        sequence=sequence,
        type=EventType.TASK_UPDATED,
        payload={},
        source=source,  # type: ignore[arg-type]
    )


class _RawEvent:
    """A bare object carrying only a ``source`` attribute.

    ``derived_provenance_for_events`` is deliberately ``Any``-typed: its callers
    read events back from JSON or from projection intermediaries, where
    ``source`` is a plain string or absent. A pydantic :class:`Event` validates
    ``source`` to the enum and cannot represent those shapes, so this stand-in
    exercises the arms the real callers hit.
    """

    def __init__(self, sequence: int, source: Origin | str | None) -> None:
        self.sequence = sequence
        self.source = source


def test_derived_provenance_for_events_reads_an_origin_source() -> None:
    events = [_event(1, Origin.HUMAN), _event(2, Origin.DETERMINISTIC)]
    assert derived_provenance_for_events(events) is Origin.DETERMINISTIC


def test_derived_provenance_for_events_parses_a_string_source() -> None:
    # Events read back from JSON carry the enum value as a plain string; the
    # helper parses it rather than degrading to EXTERNAL_AGENT. A single human
    # source asserting HUMAN is what proves the parse arm ran: a broken parse
    # would fall through to EXTERNAL_AGENT and this would fail.
    events = [_RawEvent(1, Origin.HUMAN.value)]
    assert derived_provenance_for_events(events) is Origin.HUMAN


def test_derived_provenance_for_events_parses_a_string_source_to_the_weakest() -> None:
    # The parsed origins still fold through min, so a string human source
    # alongside a string external agent source degrades to the weaker one.
    events = [
        _RawEvent(1, Origin.HUMAN.value),
        _RawEvent(2, Origin.EXTERNAL_AGENT.value),
    ]
    assert derived_provenance_for_events(events) is Origin.EXTERNAL_AGENT


def test_derived_provenance_for_events_degrades_an_unknown_string_source() -> None:
    # Chosen behaviour for an ambiguous raw value: an unparsable source is
    # treated as an external agent claim, never as verified.
    events = [_RawEvent(1, Origin.HUMAN.value), _RawEvent(2, "not-a-real-origin")]
    assert derived_provenance_for_events(events) is Origin.EXTERNAL_AGENT


def test_derived_provenance_for_events_degrades_an_absent_source() -> None:
    # A None source (a hand-built dict, or an event with no source field) also
    # degrades rather than being skipped, so the fold stays monotone.
    events = [_RawEvent(1, Origin.HUMAN), _RawEvent(2, None)]
    assert derived_provenance_for_events(events) is Origin.EXTERNAL_AGENT


class _AttributelessEvent:
    """An object with a sequence but no ``source`` attribute at all.

    ``getattr(e, "source", None)`` reads this the same as an explicit None, but
    the two shapes reach the helper from different callers: a projection
    intermediary may simply omit the field. Pinning it keeps the ``getattr``
    default from silently becoming a skip.
    """

    def __init__(self, sequence: int) -> None:
        self.sequence = sequence


def test_derived_provenance_for_events_degrades_a_missing_source_attribute() -> None:
    events = [_RawEvent(1, Origin.HUMAN), _AttributelessEvent(2)]
    assert derived_provenance_for_events(events) is Origin.EXTERNAL_AGENT


def test_derived_provenance_for_events_is_external_agent_when_empty() -> None:
    assert derived_provenance_for_events([]) is Origin.EXTERNAL_AGENT


def test_clamp_derived_origin_caps_a_claim_at_its_source() -> None:
    # A human-verified claim folded over an external-agent source cannot keep
    # the higher trust: the result is the weaker of the two.
    assert clamp_derived_origin(Origin.HUMAN, Origin.EXTERNAL_AGENT) is Origin.EXTERNAL_AGENT


def test_clamp_derived_origin_caps_a_claim_at_the_weakest_seen() -> None:
    # weakest_seen is the floor the summary must not upgrade past; here it is
    # weaker than both the claim and the writer's own source.
    assert (
        clamp_derived_origin(Origin.HUMAN, Origin.DETERMINISTIC, weakest_seen=Origin.EXTERNAL_AGENT)
        is Origin.EXTERNAL_AGENT
    )


def test_clamp_derived_origin_keeps_a_supported_claim() -> None:
    # Nothing clamps when every contributor is at least as trusted as the
    # claim, so a deterministic claim over a deterministic source is unchanged.
    assert clamp_derived_origin(Origin.DETERMINISTIC, Origin.DETERMINISTIC) is Origin.DETERMINISTIC


class _FakeStorage:
    """Minimal storage stand-in for :func:`provenance_for_run`.

    Yields canned archived/live streams and optionally raises, so the merge and
    the read-failure fallbacks can be exercised without a real database.
    """

    def __init__(
        self,
        archived: list[Event] | None = None,
        live: list[Event] | None = None,
        raise_archived: bool = False,
        raise_live: bool = False,
    ) -> None:
        self._archived = archived if archived is not None else []
        self._live = live if live is not None else []
        self._raise_archived = raise_archived
        self._raise_live = raise_live

    def read_archived_events(self, run_id: str) -> list[Event]:
        if self._raise_archived:
            raise RuntimeError("archive unreadable")
        return self._archived

    def read_events(self, run_id: str) -> list[Event]:
        if self._raise_live:
            raise RuntimeError("live log unreadable")
        return self._live


def test_provenance_for_run_folds_archive_and_live_in_sequence_order() -> None:
    # The archived prefix is an external-agent claim and the live tail is
    # deterministic; the merged result must be the weaker archived origin even
    # though the tail is read second.
    storage = _FakeStorage(
        archived=[_event(1, Origin.EXTERNAL_AGENT)],
        live=[_event(200, Origin.DETERMINISTIC)],
    )
    assert provenance_for_run(storage, "r") is Origin.EXTERNAL_AGENT


def test_provenance_for_run_laundering_is_prevented_by_the_archive() -> None:
    # Reading only the live tail would report DETERMINISTIC; the archive is
    # what keeps an archived agent fact from being laundered.
    storage = _FakeStorage(live=[_event(200, Origin.DETERMINISTIC)])
    assert provenance_for_run(storage, "r") is Origin.DETERMINISTIC
    storage_with_archive = _FakeStorage(
        archived=[_event(1, Origin.EXTERNAL_AGENT)],
        live=[_event(200, Origin.DETERMINISTIC)],
    )
    assert provenance_for_run(storage_with_archive, "r") is Origin.EXTERNAL_AGENT


def test_provenance_for_run_degrades_when_the_archive_is_unreadable() -> None:
    # An unreadable archive is treated as no contribution rather than raising,
    # so a compaction failure cannot block provenance computation.
    storage = _FakeStorage(
        archived=[_event(1, Origin.EXTERNAL_AGENT)],
        live=[_event(200, Origin.DETERMINISTIC)],
        raise_archived=True,
    )
    assert provenance_for_run(storage, "r") is Origin.DETERMINISTIC


def test_provenance_for_run_degrades_when_the_live_log_is_unreadable() -> None:
    storage = _FakeStorage(
        archived=[_event(1, Origin.EXTERNAL_AGENT)],
        live=[_event(200, Origin.DETERMINISTIC)],
        raise_live=True,
    )
    assert provenance_for_run(storage, "r") is Origin.EXTERNAL_AGENT


def test_provenance_for_run_is_external_agent_for_an_empty_history() -> None:
    storage = _FakeStorage()
    assert provenance_for_run(storage, "r") is Origin.EXTERNAL_AGENT


def test_absent_trust_is_unknown_not_untrusted() -> None:
    # Chosen behaviour for an ambiguous value: no trust information is not the
    # same as a known-untrusted claim, so it reports UNKNOWN and the source
    # None is preserved on the view rather than being coerced to unverified.
    view = summarize(Origin.HUMAN, StateStatus.VALID)
    assert view.trust is None
    assert view.how_trusted is CanonicalProvenance.UNKNOWN


def test_present_trust_maps_through_canonical_trust() -> None:
    # TrustLevel is a Literal of the three accepted strings, not an enum.
    view = summarize(Origin.HUMAN, StateStatus.VALID, trust="unverified")
    assert view.how_trusted is CanonicalProvenance.INFERRED


def test_absent_trust_leaves_primary_falling_back_to_the_origin() -> None:
    # A valid state with no trust information falls through to who asserted it,
    # so a human claim with no recorded trust still reads as verified by origin.
    view = summarize(Origin.HUMAN, StateStatus.VALID)
    assert view.primary is CanonicalProvenance.VERIFIED

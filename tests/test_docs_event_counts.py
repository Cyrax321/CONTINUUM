"""Guard the documented ``EventType`` count against silent drift (#1100).

``EventType`` grows with every capability that lands, and the docs kept stating
the count they happened to carry the day they were written: 29 in most places,
32 in the SVG, while the enum held 51. A reader who trusts a stale figure is
told the vocabulary is smaller than it is.

This guard pins two things to the live enum:

* every *figure* the docs state ("29 event types", "Event types (29)",
  "hash-chained, 29 types", the SVG's "32 event types") equals ``len(EventType)``;
* every list the docs label as exhaustive ("Complete list", "The complete set",
  the drawing prompt's inventory) contains exactly the enum's members.

Sample mentions ("`RUN_STARTED`, `TOOL_CALLED`, ... and more") are not treated
as exhaustive: only that the names they do cite are real members, and that the
"and N more" in ``references/architecture-diagram.md`` still adds up.

Figures are matched where the number sits directly on the phrase, so
historical prose like CHANGELOG.md's "11 new event types" (a *delta*, not a
total) is left alone. Regenerate the docs after changing ``events.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from continuum.events import EventType

ROOT = Path(__file__).resolve().parents[1]

# Docs that state the event-type count or inventory. CHANGELOG.md and the other
# historical records are deliberately absent: they cite superseded totals as
# deltas ("11 new event types") that must not be pinned to the live count.
_SCANNED_FILES = (
    ROOT / "README.md",
    *sorted(ROOT.glob("README.*.md")),  # translated READMEs
    *sorted(ROOT.joinpath("references").glob("*.md")),
    *sorted(ROOT.joinpath("docs").rglob("*.md")),
    *sorted(ROOT.joinpath("docs").rglob("*.html")),
    *sorted(ROOT.joinpath("docs").rglob("*.svg")),
)

# Every prose and markup form the docs use for the total. The number must sit
# directly against the phrase so a historical delta ("11 new event types") or
# an unrelated figure cannot match.
_FIGURE_RES = {
    "N event types": re.compile(r"(\d+)\s+event types"),
    "event types (N)": re.compile(r"event types\s*\((\d+)\)", re.IGNORECASE),
    "hash-chained, N types": re.compile(r"hash-chained,\s*(\d+)\s+types"),
    "svg text": re.compile(r">\s*(\d+)\s+event types\s*<"),
    # Translated READMEs: es/pt-BR and ja/zh-CN state the count in their own
    # wording, and they drifted to 36/44 while the English README said 51.
    "N tipos de eventos": re.compile(r"(\d+)\s+tipos\s+de\s+eventos", re.IGNORECASE),
    "N 種のイベントタイプ": re.compile(r"(\d+)\s+種のイベントタイプ"),
    "N 种事件类型": re.compile(r"(\d+)\s+种事件类型"),
}

# region (start marker, stop marker) -> the exhaustive name lists, in the order
# the docs present them.
_COMPLETE_LISTS = (
    (
        ROOT / "references" / "architecture-data.md",
        "Complete list:",
        "\n---\n",
    ),
    (
        ROOT / "references" / "architecture-spec.md",
        "The complete set:",
        "\n- ",
    ),
    (
        ROOT / "references" / "architecture-drawing-prompt.md",
        "do not list them all on the diagram",
        "\n- ",
    ),
)

_TOKEN_RES = re.compile(r"\b[A-Z][A-Z0-9_]+\b")


def _documented_figures(path: Path) -> list[tuple[str, int]]:
    """The (form, figure) pairs ``path`` states, or ``[]`` if it states none."""
    text = path.read_text(encoding="utf-8")
    found: list[tuple[str, int]] = []
    for form, rx in _FIGURE_RES.items():
        for figure in rx.findall(text):
            found.append((form, int(figure)))
    return found


def _region(path: Path, start: str, stop: str) -> str:
    text = path.read_text(encoding="utf-8")
    begin = text.find(start)
    assert begin != -1, f"{path} lost its {start!r} marker"
    end = text.find(stop, begin)
    assert end != -1, f"{path} lost its {stop!r} terminator"
    return text[begin:end]


def test_documented_event_type_count_matches_enum() -> None:
    """Every stated figure agrees with the live ``EventType`` membership."""
    expected = len(EventType)
    bad: list[str] = []
    stated = 0
    for path in _SCANNED_FILES:
        if not path.is_file():
            continue
        for form, figure in _documented_figures(path):
            stated += 1
            if figure != expected:
                bad.append(f"{path}: {form} says {figure}, enum has {expected}")
    assert stated, "no document states an event-type figure; the guard lost its spine"
    assert not bad, "\n".join(
        [
            "documented event-type count(s) disagree with EventType; re-sync the "
            "docs after changing events.py:",
            *bad,
        ]
    )


def test_documented_complete_lists_match_enum() -> None:
    """Lists labelled complete hold exactly the enum's members."""
    expected = {member.name for member in EventType}
    for path, start, stop in _COMPLETE_LISTS:
        tokens = _TOKEN_RES.findall(_region(path, start, stop))
        # A set comparison misses a duplicated entry, so length is checked too.
        assert len(tokens) == len(expected), (
            f"{path} lists {len(tokens)} names but EventType has "
            f"{len(expected)} (a duplicate or a missing member)"
        )
        got = set(tokens)
        assert got == expected, (
            f"{path} membership drifts from EventType: "
            f"missing={sorted(expected - got)}, extra={sorted(got - expected)}"
        )


def test_documented_event_type_samples_are_real() -> None:
    """Names cited as examples are real members and the remainder adds up.

    ``references/architecture-diagram.md`` shows nine names then "and N more";
    both the parenthesized total and that remainder have drifted before. The
    check stays silent if the row is ever reworded, rather than failing on a
    shape it no longer recognises.
    """
    path = ROOT / "references" / "architecture-diagram.md"
    row = next(
        (
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("| **Event types")
        ),
        None,
    )
    if row is None:
        return
    total_match = re.search(r"Event types\s*\((\d+)\)", row)
    remainder_match = re.search(r"and (\d+) more", row)
    assert total_match and remainder_match, f"{path} reworded its Event types row"

    names = re.findall(r"`([A-Z][A-Z0-9_]+)`", row)
    assert names, f"{path} Event types row cites no backticked names"
    members = {member.name for member in EventType}
    unknown = [name for name in names if name not in members]
    assert not unknown, f"{path} cites non-existent event types: {unknown}"

    total = int(total_match.group(1))
    remainder = int(remainder_match.group(1))
    assert total == len(members), (
        f"{path} says ({total}) event types but EventType has {len(members)}"
    )
    assert total == len(names) + remainder, (
        f"{path} lists {len(names)} names plus 'and {remainder} more' = "
        f"{len(names) + remainder}, not the {total} it claims"
    )

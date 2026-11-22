"""The reading of a review's `## Deferred observations` section: each entry
with the target it is addressed to, its text and the disposition the trailing
markers an older review carries give, and the joining of one task's entries
over the files that carry them."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import NamedTuple

# The section a reviewer writes and the line an entry of it begins at, as
# prompts/reviewer.md gives them; the markers' grammar is the skills', in
# orchestrator-artifacts.
SECTION_HEADING = "## Deferred observations"
ENTRY_PREFIX = "- Affects:"
_SEPARATOR = " — "

_ROUTED = "routed → "
_PROMOTED = "promoted → "
_BARE_KINDS = {
    "fixed": "fixed",
    "dismissed": "dismissed",
    "audit-corroborated": "legacy",
    "audit-dismissed": "legacy",
    "unrouted-reported": "legacy",
}
_ROADMAP_TARGET = re.compile(r"(.+) § Phase ([0-9]+)\Z")


# The closing signals an answer of a review genre ends with — the pipeline's
# completion signals. A reviewer writes the section last and closes the
# answer under it with no heading between, so a section ends at one and a
# signal never becomes an entry's text. The tokens are restated here: this
# module takes no edge to their home in `protocol` and `agents`.
_CLOSING_SIGNALS = ("REVIEW_PASS", "PLAN_REVIEW_PASS", "ESCALATION")


class Disposition(NamedTuple):
    """What a head did with an entry: the kind it disposed it in — `fixed`,
    `routed`, `dismissed` or the retired markers' `legacy` — and, for a
    routed one, the target it was routed to, `spec` the path or `roadmap`
    and `phase`; None everywhere else."""

    kind: str
    target: dict | None


class Entry(NamedTuple):
    """One deferred observation as a review file carries it: the target it
    is addressed to, its text with the continuation lines and without
    markers, and the disposition its first line's markers give — None where
    the line carries no marker."""

    affects: str
    text: str
    disposition: Disposition | None


def read_entries(text: str) -> list[Entry]:
    """The entries of a file's `## Deferred observations` section, in order.

    An entry begins at a `- Affects:` line of the section and runs to the
    next entry, the next `## ` heading or the closing signal the answer ends
    with, whichever comes first; the section ends there too. A file with no
    section has no entries, and a `- Affects:` line outside the section is
    not an entry: the section's own start and end bound the reading.
    """
    entries: list[Entry] = []
    in_section = False
    lines_of_entry: list[str] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("## "):
            if lines_of_entry is not None:
                entries.append(_entry(lines_of_entry))
                lines_of_entry = None
            in_section = line.strip() == SECTION_HEADING
        elif line.strip() in _CLOSING_SIGNALS:
            if lines_of_entry is not None:
                entries.append(_entry(lines_of_entry))
                lines_of_entry = None
            in_section = False
        elif in_section and line.startswith(ENTRY_PREFIX):
            if lines_of_entry is not None:
                entries.append(_entry(lines_of_entry))
            lines_of_entry = [line]
        elif lines_of_entry is not None:
            lines_of_entry.append(line)
    if lines_of_entry is not None:
        entries.append(_entry(lines_of_entry))
    return entries


def entry_id(segment: str | None, stem: str, affects: str, text: str) -> str:
    """An entry's opaque name: the hexadecimal digest of the task's segment
    and stem and the entry's `affects` and `text`, so an entry several of a
    task's files carry with identical text is one entry whatever file is
    read."""
    parts = (segment or "", stem, affects, text)
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def collect_entries(segment: str | None, stem: str, rounds: Iterable[tuple[str, int, str]]) -> list[dict]:
    """A task's entries over the files that carry them, in round order: the
    rounds, each its step's name, its round and its file's text, in run
    order; an entry several rounds carry with identical `affects` and text
    is one entry whose `sources` hold each file's step and round. An entry's
    disposition is the latest marker a round's copy carries; where no copy
    carries one it is None.
    """
    order: list[tuple[str, str]] = []
    sources: dict[tuple[str, str], list[dict]] = {}
    dispositions: dict[tuple[str, str], Disposition | None] = {}
    for step, number, text in rounds:
        for entry in read_entries(text):
            key = (entry.affects, entry.text)
            if key not in sources:
                order.append(key)
                sources[key] = []
                dispositions[key] = None
            sources[key].append({"step": step, "round": number})
            if entry.disposition is not None:
                dispositions[key] = entry.disposition
    listed = []
    for affects, text in order:
        disposition = dispositions[(affects, text)]
        carried: dict | None = None
        if disposition is not None:
            carried = {"kind": disposition.kind}
            if disposition.target is not None:
                carried["target"] = disposition.target
        listed.append({
            "entry": entry_id(segment, stem, affects, text),
            "affects": affects,
            "text": text,
            "sources": sources[(affects, text)],
            "disposition": carried,
        })
    return listed


def _entry(lines: list[str]) -> Entry:
    """One entry from its lines: the first line's target and observation,
    the continuation lines kept as they stand, and the disposition the first
    line's trailing markers give.

    The markers are stripped from the whole first line before the target and
    the observation are told apart, so a line a reviewer wrote with no ` — `
    separator still reads its disposition, its marker in neither part.
    """
    rest, disposition = _split_markers(lines[0][len(ENTRY_PREFIX):])
    affects, _, observation = rest.partition(_SEPARATOR)
    entry_lines = [observation, *lines[1:]]
    while entry_lines and not entry_lines[-1].strip():
        entry_lines.pop()
    return Entry(affects.strip(), "\n".join(entry_lines), disposition)


def _split_markers(line: str) -> tuple[str, Disposition | None]:
    """A first line's observation and the disposition its trailing bracketed
    markers give.

    The markers are stripped from the text, the rightmost — the latest a
    head appended — giving the disposition. A trailing bracket that no
    marker names is text and stays, with everything to its left.
    """
    text = line
    disposition = None
    while True:
        trimmed = text.rstrip()
        if not trimmed.endswith("]"):
            break
        opening = trimmed.rfind("[")
        if opening == -1:
            break
        marker = _disposition(trimmed[opening + 1:-1])
        if marker is None:
            break
        if disposition is None:
            disposition = marker
        text = trimmed[:opening]
    return text.rstrip(), disposition


def _disposition(marker: str) -> Disposition | None:
    """The disposition a bracket's content names, None where it names no
    marker.

    The live markers are `fixed`, `routed` with the target's `spec` the path
    or, ending in `§ Phase N`, its `roadmap` and `phase`, and `dismissed`;
    the retired `promoted`, `audit-corroborated`, `audit-dismissed` and
    `unrouted-reported` read as `legacy`, disposed with no target.
    """
    if marker in _BARE_KINDS:
        return Disposition(_BARE_KINDS[marker], None)
    if marker.startswith(_ROUTED):
        target_text = marker[len(_ROUTED):]
        roadmap = _ROADMAP_TARGET.fullmatch(target_text)
        if roadmap is not None:
            return Disposition("routed", {"roadmap": roadmap.group(1), "phase": int(roadmap.group(2))})
        return Disposition("routed", {"spec": target_text})
    if marker.startswith(_PROMOTED):
        return Disposition("legacy", None)
    return None

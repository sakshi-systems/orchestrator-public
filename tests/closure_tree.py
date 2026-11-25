"""A project tree of the closure tests' own: a roadmap of the caller's text
and, for a done task, the plan, the sidecar and a review whose
`## Deferred observations` section holds one entry — with no disposition, with
a trailing marker, or with a disposition record, as the caller chooses.

`run_tree.py` disposes every entry it holds, so `owed_phase` never returns a
phase over it; this tree is the one where an entry stays undisposed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

from orchestrator.artifact_layout import DISPOSITIONS_DIRNAME, PLANS_DIRNAME, REVIEWS_DIRNAME, artifact_dir
from orchestrator.dispositions import record_path
from orchestrator.layout import _artifact_subdir
from orchestrator.observations import entry_id
from orchestrator.roadmap import Task

UNDISPOSED = "undisposed"
MARKER = "marker"
RECORD = "record"


class Placed(NamedTuple):
    """A done task laid into the tree: its sequence number and slug as the
    artifacts carry them, the stem of its group, the entry its review holds
    and that entry's id."""

    seq: str
    slug: str
    stem: str
    affects: str
    text: str
    entry: str


def phase_roadmap(*phases: tuple[int, tuple[str, ...]]) -> str:
    """A roadmap text of `## Phase N — …` sections, each with its lines in
    order; a line is a task line or a `---STOP---` marker the caller writes."""
    parts = ["# Roadmap", ""]
    for number, lines in phases:
        parts.append(f"## Phase {number} — the phase {number}")
        parts.append("")
        parts.extend(lines)
        parts.append("")
    return "\n".join(parts)


def build(root: Path, *, roadmap: str, relpath: str = "ROADMAP.md") -> Path:
    """Write `roadmap` at `<root>/project/.ai-factory/<relpath>`, create the
    plans, reviews and dispositions directories — under the roadmap's segment
    where it is a named one — and return the project directory."""
    project = root / "project"
    path = project / ".ai-factory" / relpath
    path.parent.mkdir(parents=True)
    path.write_text(roadmap, encoding="utf-8")
    segment = _artifact_subdir(relpath)
    for dirname in (PLANS_DIRNAME, REVIEWS_DIRNAME, DISPOSITIONS_DIRNAME):
        artifact_dir(project, dirname, segment).mkdir(parents=True, exist_ok=True)
    return project


def lay_done_task(
    project: Path,
    title: str,
    *,
    seq: int = 1,
    relpath: str = "ROADMAP.md",
    variant: str = UNDISPOSED,
    affects: str = "unknown",
    text: str = "The greeting module holds no word of its licence.",
) -> Placed:
    """Lay one done task's artifacts into the tree: the plan, a sidecar at
    `done` and a review that ends in `REVIEW_PASS` over a deferred-observations
    section of one entry. `variant` chooses how the entry is disposed:
    `UNDISPOSED` leaves no marker and no record, `MARKER` ends the entry's
    line in `[dismissed]`, `RECORD` writes a disposition record of `fixed`."""
    segment = _artifact_subdir(relpath)
    slug = Task(title=title, description="", done=True, line_number=0).slug
    number = f"{seq:02d}"
    stem = f"{number}-{slug}"
    line = f"- Affects: {affects} — {text}"
    if variant == MARKER:
        line += " [dismissed]"
    plans = artifact_dir(project, PLANS_DIRNAME, segment)
    (plans / f"{stem}.md").write_text(f"# Plan: {title}\n\n## Steps\n\n1. Do it.\n", encoding="utf-8")
    (plans / f"{stem}.json").write_text(json.dumps({"step": "done"}), encoding="utf-8")
    reviews = artifact_dir(project, REVIEWS_DIRNAME, segment)
    (reviews / f"{stem}-review-1.md").write_text(
        f"No findings.\n\n## Deferred observations\n\n{line}\n\nREVIEW_PASS\n", encoding="utf-8",
    )
    placed = Placed(number, slug, stem, affects, text, entry_id(segment, stem, affects, text))
    if variant == RECORD:
        dispose(project, placed, relpath=relpath)
    return placed


def dispose(project: Path, placed: Placed, *, relpath: str = "ROADMAP.md") -> None:
    """Write the task's disposition record with its entry `fixed`, as a head's
    closure leaves it."""
    path = record_path(project, _artifact_subdir(relpath), placed.seq, placed.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({placed.entry: {"kind": "fixed"}}), encoding="utf-8")

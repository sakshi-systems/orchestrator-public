"""Parse and update .ai-factory/ROADMAP.md files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import RunDataRefusal

CHECKBOX_RE = re.compile(r"^- \[([ x])\] \*\*(.+?)\*\*\s*[—–-]\s*(.+)$")
PHASE_HEADING_RE = re.compile(r"^Phase (\d+)\b")
# The number is anchored to the contract line's own form: it is followed by
# the dash that joins it to the rest of the title, so a flat title that
# begins like a number ("2.0 migration notes") carries no number.
NUMBER_RE = re.compile(r"(\d+(?:\.\d+){1,2})(?=\s+[—–-]\s)")


@dataclass
class Task:
    title: str
    description: str
    done: bool
    line_number: int  # 0-based line index in file
    section: str | None = None

    @property
    def slug(self) -> str:
        """Convert title to a filename-safe slug."""
        s = self.title.lower()
        s = re.sub(r"[^a-z0-9]+", "-", s)
        return s.strip("-")

    @property
    def number(self) -> str | None:
        """The leading ``N.M`` or ``N.M.k`` of the title as the contract line
        writes it — a number the line's own dash follows — or None where the
        title carries none, a flat task."""
        found = NUMBER_RE.match(self.title)
        return found.group(1) if found else None

    @property
    def phase(self) -> int | None:
        """The ``N`` of the section heading ``Phase N —`` the task sits under,
        or None where the section names no phase."""
        if self.section is None:
            return None
        found = PHASE_HEADING_RE.match(self.section)
        return int(found.group(1)) if found else None


@dataclass
class ParseResult:
    tasks: list[Task]
    breakpoint_hit: bool
    tasks_after_breakpoint: int


def parse_roadmap(path: Path, *, keep_after_breakpoint: bool = False) -> ParseResult:
    """Parse ROADMAP.md and return ParseResult.

    When a ``---STOP---`` marker is present and at least one task follows it,
    only tasks before the marker are returned and ``breakpoint_hit`` is True,
    unless ``keep_after_breakpoint`` keeps the tasks after the marker among
    the returned ones — the run's queue hides them whatever a reader asks for.
    A marker with nothing after it is treated as if it weren't there.
    """
    lines = path.read_text().splitlines()
    tasks: list[Task] = []
    marker_found = False
    tasks_after_breakpoint = 0
    current_section: str | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            current_section = stripped.lstrip("#").strip()

        if marker_found and not keep_after_breakpoint:
            if CHECKBOX_RE.match(stripped):
                tasks_after_breakpoint += 1
            continue

        if stripped == "---STOP---":
            marker_found = True
            continue

        m = CHECKBOX_RE.match(stripped)
        if m:
            if marker_found:
                tasks_after_breakpoint += 1
            done = m.group(1) == "x"
            title = m.group(2).strip()
            description = m.group(3).strip()
            tasks.append(Task(title=title, description=description, done=done, line_number=i, section=current_section))

    breakpoint_hit = marker_found and tasks_after_breakpoint > 0
    return ParseResult(tasks=tasks, breakpoint_hit=breakpoint_hit, tasks_after_breakpoint=tasks_after_breakpoint)


def find_task(tasks: list[Task], address: str) -> Task:
    """The task an address names: its ``N.M``, or a flat task's title as its
    line gives it.

    Raises RunDataRefusal where no line of the roadmap matches the address,
    and where a flat title stands on more than one line, naming each line's
    number as the file counts them.
    """
    for task in tasks:
        if task.number == address:
            return task
    titled = [task for task in tasks if task.number is None and task.title == address]
    if len(titled) > 1:
        lines = ", ".join(str(task.line_number + 1) for task in titled)
        raise RunDataRefusal(f"Task {address!r} stands on more than one line of the roadmap: lines {lines}")
    if titled:
        return titled[0]
    raise RunDataRefusal(f"No task {address!r} in the roadmap")


def _find_task_line(lines: list[str], task: Task) -> int | None:
    """Return the 0-based index of the unchecked task line matching *task.title*.

    Strips each line before matching (mirrors ``parse_roadmap``).  Returns ``None``
    when no unchecked line with that title is found.
    """
    for i, line in enumerate(lines):
        m = CHECKBOX_RE.match(line.strip())
        if m and m.group(1) == " " and m.group(2).strip() == task.title:
            return i
    return None


def mark_done(path: Path, task: Task, elapsed_secs: int | None = None) -> None:
    """Mark a task as completed in ROADMAP.md."""
    lines = path.read_text().splitlines()
    idx = _find_task_line(lines, task)
    if idx is None:
        idx = task.line_number
    line = lines[idx]
    new_line = line.replace("- [ ]", "- [x]", 1)
    if elapsed_secs is not None:
        mins, secs = divmod(elapsed_secs, 60)
        hours, mins = divmod(mins, 60)
        time_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
        new_line = new_line.rstrip() + f" [{time_str}]"
    lines[idx] = new_line
    path.write_text("\n".join(lines) + "\n")

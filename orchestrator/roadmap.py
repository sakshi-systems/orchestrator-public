"""Parse and update .ai-factory/ROADMAP.md files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CHECKBOX_RE = re.compile(r"^- \[([ x])\] \*\*(.+?)\*\*\s*[—–-]\s*(.+)$")


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


@dataclass
class ParseResult:
    tasks: list[Task]
    breakpoint_hit: bool
    tasks_after_breakpoint: int


def parse_roadmap(path: Path) -> ParseResult:
    """Parse ROADMAP.md and return ParseResult.

    When a ``---STOP---`` marker is present and at least one task follows it,
    only tasks before the marker are returned and ``breakpoint_hit`` is True.
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

        if marker_found:
            if CHECKBOX_RE.match(stripped):
                tasks_after_breakpoint += 1
            continue

        if stripped == "---STOP---":
            marker_found = True
            continue

        m = CHECKBOX_RE.match(stripped)
        if m:
            done = m.group(1) == "x"
            title = m.group(2).strip()
            description = m.group(3).strip()
            tasks.append(Task(title=title, description=description, done=done, line_number=i, section=current_section))

    breakpoint_hit = marker_found and tasks_after_breakpoint > 0
    return ParseResult(tasks=tasks, breakpoint_hit=breakpoint_hit, tasks_after_breakpoint=tasks_after_breakpoint)


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


def mark_skipped(path: Path, task: Task) -> None:
    """Mark a task as skipped (already done) in ROADMAP.md."""
    lines = path.read_text().splitlines()
    idx = _find_task_line(lines, task)
    if idx is None:
        idx = task.line_number
    line = lines[idx]
    lines[idx] = line.replace("- [ ]", "- [x] ⚠️ SKIPPED (already implemented)", 1)
    path.write_text("\n".join(lines) + "\n")

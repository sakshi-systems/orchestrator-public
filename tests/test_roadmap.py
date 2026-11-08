"""Unit tests for parse_roadmap, Task.slug, _find_task_line, mark_done, and mark_skipped."""

from pathlib import Path

from orchestrator.roadmap import _find_task_line, mark_done, mark_skipped, parse_roadmap, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_roadmap(tmp_path: Path, lines: list) -> Path:
    """Write lines to tmp_path/ROADMAP.md joined by newlines and return the path."""
    path = tmp_path / "ROADMAP.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Task 1: parse_roadmap — task collection and done flags
# ---------------------------------------------------------------------------


def test_parse_roadmap_done_flags(tmp_path):
    """Should set done=True for [x] lines and done=False for [ ] lines when the file mixes both."""
    path = _write_roadmap(tmp_path, [
        "- [x] **Done task** — some description",
        "- [ ] **Pending task** — another description",
    ])
    result = parse_roadmap(path)
    assert len(result.tasks) == 2
    assert result.tasks[0].done is True
    assert result.tasks[1].done is False


def test_parse_roadmap_skips_non_checkbox_lines(tmp_path):
    """Should not collect a line that has no **title** checkbox pattern."""
    path = _write_roadmap(tmp_path, [
        "Some prose line",
        "- just a plain bullet",
        "- [ ] **Real task** — real description",
    ])
    result = parse_roadmap(path)
    assert len(result.tasks) == 1
    assert result.tasks[0].title == "Real task"


def test_parse_roadmap_line_numbers(tmp_path):
    """Should record the 0-based line_number for each parsed task matching its position in the file."""
    path = _write_roadmap(tmp_path, [
        "# Heading",
        "Some prose",
        "- [ ] **First** — description one",
        "Some other text",
        "- [x] **Second** — description two",
    ])
    result = parse_roadmap(path)
    assert len(result.tasks) == 2
    assert result.tasks[0].line_number == 2
    assert result.tasks[1].line_number == 4


# ---------------------------------------------------------------------------
# Task 2: parse_roadmap — section assignment
# ---------------------------------------------------------------------------


def test_parse_roadmap_section_from_h2(tmp_path):
    """Should set section="Phase name" on tasks that follow a "## Phase name" heading."""
    path = _write_roadmap(tmp_path, [
        "## Phase name",
        "- [ ] **Task A** — description",
    ])
    result = parse_roadmap(path)
    assert result.tasks[0].section == "Phase name"


def test_parse_roadmap_section_from_h3(tmp_path):
    """Should also assign section from a "### Subheading" heading."""
    path = _write_roadmap(tmp_path, [
        "### Subheading",
        "- [ ] **Task B** — description",
    ])
    result = parse_roadmap(path)
    assert result.tasks[0].section == "Subheading"


def test_parse_roadmap_section_none_before_heading(tmp_path):
    """Should leave section=None on tasks that appear before any heading."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **Early task** — description",
        "## Some Phase",
        "- [ ] **Later task** — description",
    ])
    result = parse_roadmap(path)
    assert result.tasks[0].section is None
    assert result.tasks[1].section == "Some Phase"


# ---------------------------------------------------------------------------
# Task 3: parse_roadmap — ---STOP--- breakpoint behavior
# ---------------------------------------------------------------------------


def test_parse_roadmap_breakpoint_hit_excludes_after_marker(tmp_path):
    """Should set breakpoint_hit=True and exclude tasks after the marker when ---STOP--- is followed by task lines."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **Before stop** — description",
        "---STOP---",
        "- [ ] **After stop** — description",
    ])
    result = parse_roadmap(path)
    assert result.breakpoint_hit is True
    assert len(result.tasks) == 1
    assert result.tasks[0].title == "Before stop"


def test_parse_roadmap_tasks_after_breakpoint_count(tmp_path):
    """Should set tasks_after_breakpoint to the count of task lines following the marker."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **Before stop** — description",
        "---STOP---",
        "- [ ] **After stop 1** — description",
        "- [x] **After stop 2** — description",
    ])
    result = parse_roadmap(path)
    assert result.tasks_after_breakpoint == 2


def test_parse_roadmap_breakpoint_false_when_nothing_after(tmp_path):
    """Should set breakpoint_hit=False when ---STOP--- is the last non-blank line with nothing after it."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **Task** — description",
        "---STOP---",
    ])
    result = parse_roadmap(path)
    assert result.breakpoint_hit is False
    assert result.tasks_after_breakpoint == 0


def test_parse_roadmap_no_breakpoint(tmp_path):
    """Should set breakpoint_hit=False and tasks_after_breakpoint=0 when no ---STOP--- marker is present."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **Task** — description",
    ])
    result = parse_roadmap(path)
    assert result.breakpoint_hit is False
    assert result.tasks_after_breakpoint == 0


# ---------------------------------------------------------------------------
# Task 4: Task.slug generation
# ---------------------------------------------------------------------------


def test_slug_em_dash_and_spaces():
    """Should produce "config-file-replace-env-vars" when title is "Config file — replace env vars"."""
    m = Task(title="Config file — replace env vars", description="desc", done=False, line_number=0)
    assert m.slug == "config-file-replace-env-vars"


def test_slug_preserves_digits():
    """Should preserve digits and produce "oauth2-setup" when title is "OAuth2 setup"."""
    m = Task(title="OAuth2 setup", description="desc", done=False, line_number=0)
    assert m.slug == "oauth2-setup"


def test_slug_strips_leading_trailing_hyphens():
    """Should strip leading and trailing hyphens from the slug."""
    m = Task(title="!Config!", description="desc", done=False, line_number=0)
    assert not m.slug.startswith("-")
    assert not m.slug.endswith("-")
    assert m.slug == "config"


# ---------------------------------------------------------------------------
# Task 5: _find_task_line lookup
# ---------------------------------------------------------------------------


def test_find_task_line_unchecked_match():
    """Should return the correct 0-based index when an unchecked [ ] line matches the task title exactly."""
    lines = [
        "# Heading",
        "Some text",
        "- [ ] **My Feature** — description here",
    ]
    m = Task(title="My Feature", description="description here", done=False, line_number=0)
    assert _find_task_line(lines, m) == 2


def test_find_task_line_already_checked_returns_none():
    """Should return None when a line with the same title is already checked [x]."""
    lines = [
        "- [x] **My Feature** — description here",
    ]
    m = Task(title="My Feature", description="description here", done=False, line_number=0)
    assert _find_task_line(lines, m) is None


def test_find_task_line_no_match_returns_none():
    """Should return None when no line with that title is present."""
    lines = [
        "- [ ] **Other Feature** — description",
    ]
    m = Task(title="My Feature", description="description", done=False, line_number=0)
    assert _find_task_line(lines, m) is None


# ---------------------------------------------------------------------------
# Task 6: mark_done line mutation
# ---------------------------------------------------------------------------


def test_mark_done_replaces_checkbox(tmp_path):
    """Should replace "- [ ]" with "- [x]" on the matching line when called without elapsed_secs."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **My Task** — some description",
    ])
    m = Task(title="My Task", description="some description", done=False, line_number=0)
    mark_done(path, m)
    lines = path.read_text().splitlines()
    assert lines[0].startswith("- [x]")


def test_mark_done_appends_time_minutes_seconds(tmp_path):
    """Should append " [2m 5s]" after converting the checkbox when elapsed_secs is 125."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **My Task** — some description",
    ])
    m = Task(title="My Task", description="some description", done=False, line_number=0)
    mark_done(path, m, elapsed_secs=125)
    lines = path.read_text().splitlines()
    assert lines[0].endswith("[2m 5s]")
    assert "- [x]" in lines[0]


def test_mark_done_appends_time_hours(tmp_path):
    """Should format elapsed as "{hours}h {mins}m {secs}s" when elapsed_secs exceeds one hour."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **My Task** — some description",
    ])
    m = Task(title="My Task", description="some description", done=False, line_number=0)
    mark_done(path, m, elapsed_secs=3725)
    lines = path.read_text().splitlines()
    assert lines[0].endswith("[1h 2m 5s]")


def test_mark_done_resolves_stale_line_number(tmp_path):
    """Should locate the line via _find_task_line and mark line 3 when task.line_number is a stale 0 but the title sits on line 3."""
    path = _write_roadmap(tmp_path, [
        "# Heading",
        "Some prose",
        "- [ ] **Another Task** — description",
        "- [ ] **My Task** — some description",
    ])
    m = Task(title="My Task", description="some description", done=False, line_number=0)
    mark_done(path, m)
    lines = path.read_text().splitlines()
    assert lines[3].startswith("- [x]")
    assert "My Task" in lines[3]
    assert lines[0] == "# Heading"


def test_mark_done_leaves_other_lines_unchanged(tmp_path):
    """Should leave other lines in the file unchanged after marking one task done."""
    path = _write_roadmap(tmp_path, [
        "# Roadmap",
        "- [ ] **Task A** — description A",
        "- [ ] **Task B** — description B",
    ])
    m = Task(title="Task A", description="description A", done=False, line_number=1)
    mark_done(path, m)
    lines = path.read_text().splitlines()
    assert lines[0] == "# Roadmap"
    assert lines[2] == "- [ ] **Task B** — description B"


# ---------------------------------------------------------------------------
# Task 7: mark_skipped line mutation
# ---------------------------------------------------------------------------


def test_mark_skipped_replaces_checkbox_preserves_description(tmp_path):
    """Should replace "- [ ]" with "- [x] ⚠️ SKIPPED (already implemented)" preserving the rest of the line."""
    path = _write_roadmap(tmp_path, [
        "- [ ] **My Task** — original description",
    ])
    m = Task(title="My Task", description="original description", done=False, line_number=0)
    mark_skipped(path, m)
    lines = path.read_text().splitlines()
    assert "- [x] ⚠️ SKIPPED (already implemented)" in lines[0]
    assert "original description" in lines[0]


def test_mark_skipped_resolves_stale_line_number(tmp_path):
    """Should mark the title-matched line even when task.line_number is stale, resolving via _find_task_line."""
    path = _write_roadmap(tmp_path, [
        "# Heading",
        "- [ ] **Actual Task** — description",
    ])
    m = Task(title="Actual Task", description="description", done=False, line_number=0)
    mark_skipped(path, m)
    lines = path.read_text().splitlines()
    assert "⚠️ SKIPPED" in lines[1]
    assert lines[0] == "# Heading"

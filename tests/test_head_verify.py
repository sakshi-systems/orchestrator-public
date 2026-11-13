"""The head's verify kind: the implementer's turn must leave the project changed outside .ai-factory/."""

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator import pipeline as pipeline_module
from orchestrator.agents import HEAD_VERIFY, HeadVerifier
from orchestrator.config import OrchestratorConfig
from orchestrator.errors import PipelineStopError
from orchestrator.pipeline import IMPLEMENT_MODE, process_task
from orchestrator.ports import AgentRole
from orchestrator.protocol import _read_sessions
from tests.fakes import FakeAgentBackend, target_path

HEAD_MODE = IMPLEMENT_MODE._replace(verify=HEAD_VERIFY)
SLUG = "slug"
PLAN_NAME = f"01-{SLUG}.md"


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(project), check=True, capture_output=True)


def _head_project(tmp_path: Path, *, symlinked: bool = True) -> tuple[Path, Path]:
    """A real git repository whose .ai-factory is a symlink to a directory outside it.

    With `symlinked=False` .ai-factory is a real directory inside the repository, which makes
    the identity's local exclusion of .ai-factory the only thing keeping its files invisible.
    """
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "artifacts-home" if symlinked else project / ".ai-factory"
    for name in ("plans", "plan-reviews", "reviews"):
        (outside / name).mkdir(parents=True)
    if symlinked:
        (project / ".ai-factory").symlink_to(outside)
    (outside / "ROADMAP.md").write_text("# Work\n\n- [ ] **Some task** — Some description\n")
    (project / "tracked.txt").write_text("one\n")
    (project / ".gitignore").write_text("*.log\n")
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "add", "tracked.txt", ".gitignore")
    _git(project, "commit", "-m", "initial")
    return project, outside


def _seed_plan(outside: Path, step: str) -> Path:
    plan = outside / "plans" / PLAN_NAME
    plan.write_text("# Plan\n\n## Test Command\n`true`\n")
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": step}))
    return plan


class _TaskStub:
    slug = SLUG
    title = "Some task"
    description = "Some description"
    line_number = 2


def _neutralize(monkeypatch, *, commit) -> None:
    monkeypatch.setattr(pipeline_module, "report", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module.state, "tasks_done", 0)
    if commit:
        def _no_commit(*a, **k):
            raise AssertionError("commit must not run")
        monkeypatch.setattr(pipeline_module, "_git_commit", _no_commit)
    else:
        monkeypatch.setattr(pipeline_module, "_git_commit", lambda *a, **k: None)


def _config(plan_review_iterations=None) -> OrchestratorConfig:
    config = OrchestratorConfig(max_iterations=3, usage_threshold_5h=90, usage_threshold_weekly=95, enable_phase_sessions=False)
    config.plan_review_iterations = plan_review_iterations
    return config


def _flow_backend() -> FakeAgentBackend:
    """Write the plan and a passing plan review; the implementer's turn changes nothing."""

    def _write(call):
        if call.role is AgentRole.IMPLEMENTER:
            return
        path = target_path(call.prompt)
        if call.role is AgentRole.PLAN_REVIEWER:
            path.write_text("PLAN_REVIEW_PASS")
        elif path.parent.name == "plans":
            path.write_text("# Plan\n\n## Test Command\n`true`\n")

    return FakeAgentBackend(on_call=_write)


# ---------------------------------------------------------------------------
# The check itself, against real git
# ---------------------------------------------------------------------------


def _begin(project: Path, plan: Path) -> HeadVerifier:
    verifier = HeadVerifier(project)
    verifier.begin_turn(plan)
    return verifier


def _assert_stops(verifier: HeadVerifier, project: Path, plan: Path) -> None:
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": "implemented:1"}))
    with pytest.raises(PipelineStopError) as exc:
        verifier.verify(plan, project / "nowhere.md", None)
    assert str(exc.value) == (
        "Implement failed\n\n"
        "The implementer's turn left the project unchanged outside .ai-factory/\n\n"
        f"Plan: {plan}"
    )
    assert _read_sessions(plan)["step"] == "plan_reviewed"


def test_new_untracked_file_in_new_directory_passes(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    verifier = _begin(project, plan)

    (project / "new-dir").mkdir()
    (project / "new-dir" / "file.txt").write_text("content")

    assert verifier.verify(plan, project / "nowhere.md", None) is True


def test_edit_to_tracked_file_passes(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    verifier = _begin(project, plan)

    (project / "tracked.txt").write_text("one\ntwo\n")

    assert verifier.verify(plan, project / "nowhere.md", None) is True


def test_further_edit_to_already_modified_file_passes(tmp_path):
    """A content identity, not a status line: the file was already modified when the turn began."""
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    (project / "tracked.txt").write_text("one\ntwo\n")
    verifier = _begin(project, plan)

    (project / "tracked.txt").write_text("one\ntwo\nthree\n")

    assert verifier.verify(plan, project / "nowhere.md", None) is True


def test_unchanged_tree_stops(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    verifier = _begin(project, plan)

    _assert_stops(verifier, project, plan)


def test_change_only_under_ai_factory_stops(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    verifier = _begin(project, plan)

    (outside / "reviews" / "head-note.md").write_text("the head's own file")

    _assert_stops(verifier, project, plan)


def test_change_only_under_a_real_ai_factory_directory_stops(tmp_path):
    """With .ai-factory a real directory, only the identity's local exclusion keeps it invisible."""
    project, outside = _head_project(tmp_path, symlinked=False)
    plan = _seed_plan(outside, "plan_reviewed")
    verifier = _begin(project, plan)

    (outside / "plans" / "extra-note.md").write_text("written during the turn")

    _assert_stops(verifier, project, plan)


def test_tree_dirty_before_the_turn_and_left_as_it_was_stops(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    (project / "tracked.txt").write_text("one\ndirty\n")
    verifier = _begin(project, plan)

    _assert_stops(verifier, project, plan)


def test_ignored_file_written_during_the_turn_is_not_a_change(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    verifier = _begin(project, plan)

    (project / "noise.log").write_text("ignored by git")

    _assert_stops(verifier, project, plan)


def test_real_index_is_identical_before_and_after(tmp_path):
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "plan_reviewed")
    index = project / ".git" / "index"
    before = index.read_bytes()
    verifier = _begin(project, plan)

    (project / "new.txt").write_text("x")
    assert verifier.verify(plan, project / "nowhere.md", None) is True

    assert index.read_bytes() == before


# ---------------------------------------------------------------------------
# Through process_task with the fake backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("budget,expected_step", [(None, "plan_reviewed"), (0, "planned:1")])
def test_head_turn_that_changes_nothing_stops_the_run(tmp_path, monkeypatch, budget, expected_step):
    _neutralize(monkeypatch, commit=True)
    project, outside = _head_project(tmp_path)
    backend = _flow_backend()

    with pytest.raises(PipelineStopError) as exc:
        process_task(project, _TaskStub(), 1, _config(budget), HEAD_MODE, backend=backend)

    plan = project / ".ai-factory" / "plans" / PLAN_NAME
    assert str(exc.value) == (
        "Implement failed\n\n"
        "The implementer's turn left the project unchanged outside .ai-factory/\n\n"
        f"Plan: {plan}"
    )
    assert _read_sessions(plan)["step"] == expected_step
    assert "- [ ]" in (outside / "ROADMAP.md").read_text()
    log = subprocess.run(["git", "log", "--oneline"], cwd=str(project), capture_output=True, text=True)
    assert len(log.stdout.splitlines()) == 1


def test_head_rerun_reuses_the_implementer_session_and_closes_once_changed(tmp_path, monkeypatch):
    _neutralize(monkeypatch, commit=False)
    project, outside = _head_project(tmp_path)
    with pytest.raises(PipelineStopError):
        process_task(project, _TaskStub(), 1, _config(), HEAD_MODE, backend=_flow_backend())
    plan = project / ".ai-factory" / "plans" / PLAN_NAME
    saved = _read_sessions(plan)["implementer"]

    calls = []

    def _write(call):
        if call.role is AgentRole.IMPLEMENTER:
            calls.append(call)
            (project / "implemented.txt").write_text("work done")

    backend = FakeAgentBackend(on_call=_write)

    process_task(project, _TaskStub(), 1, _config(), HEAD_MODE, backend=backend)

    assert len(calls) == 1
    assert calls[0].session_id == saved
    assert calls[0].prompt.endswith(f"Implement the plan at: {plan}")
    assert "feedback" not in calls[0].prompt
    assert _read_sessions(plan)["step"] == "done"
    assert "- [x]" in (outside / "ROADMAP.md").read_text()


def test_head_resume_at_implemented_closes_without_the_check(tmp_path, monkeypatch):
    _neutralize(monkeypatch, commit=False)
    project, outside = _head_project(tmp_path)
    plan = _seed_plan(outside, "implemented:1")
    backend = FakeAgentBackend()

    process_task(project, _TaskStub(), 1, _config(), HEAD_MODE, backend=backend)

    assert backend.calls == []
    assert _read_sessions(plan)["step"] == "done"
    assert "- [x]" in (outside / "ROADMAP.md").read_text()

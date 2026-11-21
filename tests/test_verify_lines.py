"""The pass and fail lines the run prints: each names the path where its kind
writes an artifact."""

from pathlib import Path

import pytest

from orchestrator import pipeline as pipeline_module
from orchestrator.agents import REVIEW_VERIFY, TEST_RUN_VERIFY
from orchestrator.config import OrchestratorConfig
from orchestrator.errors import PipelineStopError
from orchestrator.implementer_result import WorkingTreeResult
from orchestrator.pipeline import IMPLEMENT_MODE, TEST_MODE, process_task
from orchestrator.ports import AgentRole
from tests.fakes import FakeAgentBackend

SLUG = "slug"
PLAN_NAME = f"01-{SLUG}.md"


def _git(project: Path, *args: str, cwd: Path | None = None) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=str(cwd or project), check=True, capture_output=True)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    """A real git repository whose .ai-factory is a symlink to a directory
    outside it, with the plan directories, a roadmap and a tracked file."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "artifacts-home"
    for name in ("plans", "plan-reviews", "reviews"):
        (outside / name).mkdir(parents=True)
    (project / ".ai-factory").symlink_to(outside)
    (outside / "ROADMAP.md").write_text("# Work\n\n- [ ] **Some task** — Some description\n")
    (project / "tracked.txt").write_text("one\n")
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "add", "tracked.txt")
    _git(project, "commit", "-m", "initial")
    origin = project.parent / f"{project.name}-origin.git"
    _git(project.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "origin", "HEAD:refs/heads/main")
    return project, outside


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


def _config() -> OrchestratorConfig:
    return OrchestratorConfig(max_iterations=3, usage_threshold_5h=90, usage_threshold_weekly=95,
                              enable_phase_sessions=False)


def _run_backend(*, review_text: str = "REVIEW_PASS", test_command: str = "true",
                 implement: Path | None = None) -> FakeAgentBackend:
    """Write the plan and answer the plan review; the reviewer writes `review_text`; the
    implementer writes `implement` when given, so its turn changes the project."""

    def _write(call):
        if call.role is AgentRole.IMPLEMENTER:
            if implement is not None:
                implement.write_text("work done")
            return None
        if call.role is AgentRole.PLAN_REVIEWER:
            return "PLAN_REVIEW_PASS"
        if call.role is AgentRole.CODE_REVIEWER and "Review the CODE CHANGES" in call.prompt:
            return review_text
        return f"# Plan\n\n## Test Command\n`{test_command}`\n"

    return FakeAgentBackend(on_call=_write)


def test_the_implement_mode_carries_the_review_kind_and_the_test_mode_the_test_run_kind():
    assert IMPLEMENT_MODE.verify is REVIEW_VERIFY
    assert TEST_MODE.verify is TEST_RUN_VERIFY


def test_review_pass_line_names_the_review_file(tmp_path, monkeypatch, capsys):
    _neutralize(monkeypatch, commit=False)
    project, _outside = _project(tmp_path)

    process_task(project, _TaskStub(), 1, _config(), IMPLEMENT_MODE,
                 backend=_run_backend(implement=project / "implemented.txt"), implementer_result=WorkingTreeResult())
    lines = capsys.readouterr().out.splitlines()

    review = project / ".ai-factory" / "reviews" / "01-slug-review-1.md"
    assert review.exists()
    assert f">>> REVIEW PASSED — see {review}" in lines


def test_test_run_pass_line_names_the_test_output_file(tmp_path, monkeypatch, capsys):
    _neutralize(monkeypatch, commit=False)
    project, outside = _project(tmp_path)
    (outside / "ROADMAP_TESTS.md").write_text("# Work\n\n- [ ] **Some task** — Some description\n")

    process_task(project, _TaskStub(), 1, _config(), TEST_MODE, backend=_run_backend(), implementer_result=WorkingTreeResult())
    lines = capsys.readouterr().out.splitlines()

    test_output = project / ".ai-factory" / "test-runs" / "01-slug-test-1.txt"
    assert test_output.exists()
    assert f">>> TESTS PASSED — see {test_output}" in lines


def test_review_fail_line_names_the_review_file(tmp_path, monkeypatch, capsys):
    _neutralize(monkeypatch, commit=False)
    project, _outside = _project(tmp_path)

    with pytest.raises(PipelineStopError):
        process_task(project, _TaskStub(), 1, _config(), IMPLEMENT_MODE,
                     backend=_run_backend(review_text="Actual findings.", implement=project / "implemented.txt"),
                     implementer_result=WorkingTreeResult())
    lines = capsys.readouterr().out.splitlines()

    review = project / ".ai-factory" / "reviews" / "01-slug-review-1.md"
    assert review.exists()
    assert f">>> Review found issues — see {review}" in lines


def test_test_run_fail_line_names_the_test_output_file(tmp_path, monkeypatch, capsys):
    _neutralize(monkeypatch, commit=False)
    project, outside = _project(tmp_path)
    (outside / "ROADMAP_TESTS.md").write_text("# Work\n\n- [ ] **Some task** — Some description\n")

    with pytest.raises(PipelineStopError):
        process_task(project, _TaskStub(), 1, _config(), TEST_MODE,
                     backend=_run_backend(test_command="exit 1"), implementer_result=WorkingTreeResult())
    lines = capsys.readouterr().out.splitlines()

    test_output = project / ".ai-factory" / "test-runs" / "01-slug-test-1.txt"
    assert test_output.exists()
    assert f">>> Tests failed — see {test_output}" in lines

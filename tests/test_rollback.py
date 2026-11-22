"""The `rollback` command against a real git repository: each position's
records, artifacts and working tree, the positions `show` lists for each
state, the refusals, and a crash injected after each step with the command
repeated to the same end state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import run_data
from orchestrator.config import OrchestratorConfig
from orchestrator.errors import RunDataRefusal
from orchestrator.iteration_ref import make_iteration_commit, push_iteration_ref
from tests.conftest import git_in

REPO = Path(__file__).resolve().parents[1]

ROADMAP = """# Roadmap

## Phase 1 — doors

- [ ] **1.1 — Door** — the task with an attempt
- [ ] **1.2 — Plain** — a task with no attempt
- [x] **1.3 — Closed** — a task whose line is checked
"""

DONE_ROADMAP_LINE = "- [x] **1.1 — Door** — the task with an attempt"

RECORD_ON = {
    "runtime": {"planner": {"runtime": "claude"}, "implementer": {"runtime": "claude"},
                "code_reviewer": {"runtime": "claude"}, "plan_reviewer": {"runtime": "claude"}},
    "planner": "plan-session",
    "implementer": "impl-session",
    "code-reviewer": "review-session",
    "plan-reviewer-1": "plan-review-session",
    "elapsed": "42",
    "step": "implemented:1",
    "escalation": "reviewer: a stop the position clears (reviews/01-1-1-door-review-1.md)",
}

RECORD_OFF = {
    "runtime": {"implementer": {"runtime": "claude"}, "code_reviewer": {"runtime": "claude"}},
    "implementer": "impl-session",
    "code-reviewer": "review-session",
    "elapsed": "7",
    "step": "implemented:1",
    "escalation": "reviewer: a stop the position clears (reviews/01-1-1-door-review-1.md)",
}


def _project(tmp_path: Path, *, record: dict | None = RECORD_ON, tree_change: bool = True,
             iteration: bool = True, done: bool = False) -> Path:
    """A project repository holding one task's attempt: the roadmap, a base
    commit of tracked work, the group's every artifact, a record, a change in
    the working tree and the attempt's iteration ref — the state a run leaves
    at the verification of its first iteration."""
    project = tmp_path / "project"
    factory = project / ".ai-factory"
    (factory / "plans").mkdir(parents=True)
    roadmap = ROADMAP.replace("- [ ] **1.1 — Door**", DONE_ROADMAP_LINE) if done else ROADMAP
    (factory / "ROADMAP.md").write_text(roadmap, encoding="utf-8")

    (project / "tracked.txt").write_text("base\n")
    (project / "gone-after.txt").write_text("a file the attempt deletes\n")
    (project / "ignored-tracked.txt").write_text("tracked though ignored\n")
    (project / ".gitignore").write_text("*.log\n.env\nignored-tracked.txt\n", encoding="utf-8")
    git_in(project, "init", "-q", "-b", "main", ".")
    git_in(project, "config", "user.email", "test@example.com")
    git_in(project, "config", "user.name", "Test")
    git_in(project, "add", "tracked.txt", "gone-after.txt", ".gitignore")
    git_in(project, "add", "-f", "ignored-tracked.txt")  # tracked from the base, though ignored after
    git_in(project, "commit", "-q", "-m", "the base")
    base = git_in(project, "rev-parse", "HEAD").strip()
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git_in(project, "remote", "add", "origin", str(origin))
    git_in(project, "push", "-q", "origin", "HEAD:refs/heads/main")

    # The attempt's artifacts, every kind one round or two.
    (factory / "plan-reviews").mkdir()
    (factory / "reviews").mkdir()
    (factory / "test-runs").mkdir()
    (factory / "plans" / "01-1-1-door.md").write_text("# Plan: 1.1 — Door\n\nThe plan.\n", encoding="utf-8")
    (factory / "plan-reviews" / "01-1-1-door-plan-review-1.md").write_text("The plan names its steps.\n\nPLAN_REVIEW_PASS\n", encoding="utf-8")
    (factory / "plan-reviews" / "01-1-1-door-plan-review-2.md").write_text("A second attempt's review.\n", encoding="utf-8")
    (factory / "reviews" / "01-1-1-door-implementer-1.md").write_text("Implemented the door.\n", encoding="utf-8")
    (factory / "reviews" / "01-1-1-door-review-1.md").write_text("No findings.\n\nREVIEW_PASS\n", encoding="utf-8")
    (factory / "test-runs" / "01-1-1-door-test-1.txt").write_text("1 passed\n", encoding="utf-8")
    (factory / "plans" / "01-1-1-door.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    if tree_change:
        # The implementer's change: a modification, a staged addition, a
        # deletion, an untracked file — a tracked file the exclude rules
        # match, changed like any tracked file — and two ignored files the
        # base lacks.
        (project / "tracked.txt").write_text("changed by the attempt\n")
        (project / "staged.txt").write_text("staged by the attempt\n")
        git_in(project, "add", "staged.txt")
        git_in(project, "rm", "-q", "gone-after.txt")
        (project / "ignored-tracked.txt").write_text("changed though ignored\n")
        (project / "untracked.txt").write_text("untracked work of the attempt\n")
        (project / "sub").mkdir()
        (project / "sub" / "work.txt").write_text("work in a new directory\n")
        (project / "debug.log").write_text("ignored, the base lacks it\n")
        (project / ".env").write_text("ignored, the base lacks it\n")

    if iteration:
        git_in(project, "add", "-A")
        state = make_iteration_commit(project, "01-1-1-door", 1)
        push_iteration_ref(project, state)
        if tree_change:
            # Work the attempt laid after its iteration commit: unstaged dirt
            # and an untracked file no index holds.
            (project / "tracked.txt").write_text("changed further after the commit\n")
            (project / "late.txt").write_text("laid after the iteration commit\n")

    (tmp_path / "base.json").write_text("{}", encoding="utf-8")
    return project


def _rollback(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run the command as a client runs it, with the machine's base the tree
    carries."""
    environment = dict(os.environ)
    environment["ORCHESTRATOR_CONFIG"] = str(root / "base.json")
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.main", "rollback", *arguments],
        cwd=REPO, env=environment, capture_output=True,
    )


def _config() -> OrchestratorConfig:
    return OrchestratorConfig(max_iterations=3, usage_threshold_5h=90, usage_threshold_weekly=95,
                              enable_phase_sessions=False)


def _task_of(project: Path, address: str):
    from orchestrator.roadmap import find_task, parse_roadmap
    return find_task(parse_roadmap(project / ".ai-factory" / "ROADMAP.md").tasks, address)


def _positions(project: Path, address: str = "1.1") -> list[str]:
    task = _task_of(project, address)
    return run_data.positions_of(project, task, run_data.locate_task(project, "ROADMAP.md", task))


def _factory_files(project: Path) -> set[str]:
    factory = project / ".ai-factory"
    return {p.relative_to(factory).as_posix() for p in factory.rglob("*") if p.is_file()}


def _state_of(project: Path) -> tuple:
    """Everything a rollback can change: the factory's files, the record,
    the porcelain status, HEAD's tree and the run's refs. HEAD is read as
    its tree, so two identically-built repositories compare equal whatever
    their commit timestamps."""
    sidecar = project / ".ai-factory" / "plans" / "01-1-1-door.json"
    record = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None
    status = git_in(project, "status", "--porcelain")
    head_tree = git_in(project, "rev-parse", "HEAD^{tree}").strip()
    refs = git_in(project, "for-each-ref", "--format=%(refname)",
                  "refs/orchestrator-iteration/", "refs/orchestrator-laid/").split()
    return (_factory_files(project), record, status, head_tree, tuple(refs))


def _status_outside_factory(project: Path) -> list[str]:
    """The porcelain status lines of paths outside `.ai-factory`: the run's
    own staging of its artifacts under `.ai-factory` — which its iteration
    commit holds — is none of the rollback's concern."""
    return [
        line for line in git_in(project, "status", "--porcelain").splitlines()
        if not line.split()[-1].startswith(".ai-factory")
    ]


# ---------------------------------------------------------------------------
# positions_of, for each state
# ---------------------------------------------------------------------------


def test_a_task_with_no_attempt_lists_none(tmp_path):
    assert _positions(_project(tmp_path), "1.2") == []


def test_a_done_task_lists_none(tmp_path):
    project = _project(tmp_path, done=True)
    assert _positions(project) == []


def test_a_planning_on_task_with_a_clean_tree_lists_start_and_plan_review(tmp_path):
    project = _project(tmp_path, tree_change=False, iteration=False)
    assert _positions(project) == ["start", "plan-review"]


def test_a_planning_on_task_with_a_changed_tree_lists_verify_beside(tmp_path):
    assert _positions(_project(tmp_path)) == ["start", "plan-review", "verify"]


def test_a_planning_off_task_with_a_changed_tree_lists_start_verify_implement(tmp_path):
    assert _positions(_project(tmp_path, record=RECORD_OFF)) == ["start", "verify", "implement"]


def test_a_planning_off_task_with_a_clean_tree_lists_start_and_implement(tmp_path):
    project = _project(tmp_path, record=RECORD_OFF, tree_change=False, iteration=False)
    assert _positions(project) == ["start", "implement"]


def test_a_tracked_change_the_exclude_rules_match_still_lists_verify(tmp_path):
    """An untracked ignored file is no change git lists; a tracked file the
    exclude rules match still is, so `verify` reads it as one while `start`
    never touches the path."""
    project = _project(tmp_path, tree_change=False, iteration=False)
    (project / "debug.log").write_text("ignored, the base lacks it\n")
    (project / "ignored-tracked.txt").write_text("changed though ignored\n")
    assert _positions(project) == ["start", "plan-review", "verify"]


def test_a_project_git_cannot_read_holds_no_change(tmp_path):
    project = _project(tmp_path, tree_change=False, iteration=False)
    git_in(project, "rev-parse", "--verify", "--quiet", "HEAD")  # a repository, then not one
    (project / ".git").rename(tmp_path / "set-aside.git")
    (project / "work.txt").write_text("a change with no repository to read it\n")
    assert _positions(project) == ["start", "plan-review"]


# ---------------------------------------------------------------------------
# rollback, each position's records, artifacts and tree
# ---------------------------------------------------------------------------


def test_plan_review_leaves_the_plan_and_the_tree_and_rewrites_the_record(tmp_path):
    project = _project(tmp_path)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "plan-review")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "plan-review"\n}\n'
    record = json.loads((project / ".ai-factory" / "plans" / "01-1-1-door.json").read_text(encoding="utf-8"))
    assert record == {
        "runtime": RECORD_ON["runtime"],
        "planner": "plan-session",
        "plan-reviewer-1": "plan-review-session",
        "elapsed": "42",
        "step": "planned:1",
    }
    assert (project / ".ai-factory" / "plans" / "01-1-1-door.md").exists()  # the plan stays
    assert _factory_files(project) == {
        "ROADMAP.md", "plans/01-1-1-door.md", "plans/01-1-1-door.json",
    }
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "changed further after the commit\n"
    assert git_in(project, "for-each-ref", "--format=%(refname)",
                  "refs/orchestrator-iteration/").split() == ["refs/orchestrator-iteration/01-1-1-door"]


def test_verify_keeps_the_plan_reviews_and_the_implementer_s_session(tmp_path):
    project = _project(tmp_path)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "verify")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "verify"\n}\n'
    record = json.loads((project / ".ai-factory" / "plans" / "01-1-1-door.json").read_text(encoding="utf-8"))
    # The full record: the implementer's session stays, the code reviewer's —
    # its reviews discarded — and the escalation go.
    assert record == {
        "runtime": RECORD_ON["runtime"],
        "planner": "plan-session",
        "implementer": "impl-session",
        "plan-reviewer-1": "plan-review-session",
        "elapsed": "42",
        "step": "implemented:1",
    }
    assert _factory_files(project) == {
        "ROADMAP.md", "plans/01-1-1-door.md", "plans/01-1-1-door.json",
        "plan-reviews/01-1-1-door-plan-review-1.md",
        "plan-reviews/01-1-1-door-plan-review-2.md",
    }
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "changed further after the commit\n"


def test_implement_returns_the_planning_off_record_to_planning(tmp_path):
    project = _project(tmp_path, record=RECORD_OFF)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "implement")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "implement"\n}\n'
    record = json.loads((project / ".ai-factory" / "plans" / "01-1-1-door.json").read_text(encoding="utf-8"))
    assert record == {
        "runtime": RECORD_OFF["runtime"],
        "elapsed": "7",
        "step": "planning:1",
    }
    assert _factory_files(project) == {
        "ROADMAP.md", "plans/01-1-1-door.md", "plans/01-1-1-door.json",
        "plan-reviews/01-1-1-door-plan-review-1.md",
        "plan-reviews/01-1-1-door-plan-review-2.md",
    }
    assert (project / "untracked.txt").exists()  # the working tree stays


def test_start_discards_everything_of_the_attempt_and_restores_the_tree(tmp_path):
    project = _project(tmp_path)
    base = git_in(project, "rev-parse", "HEAD").strip()
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "start"\n}\n'
    assert _factory_files(project) == {"ROADMAP.md"}
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert (project / "gone-after.txt").exists()  # restored: the attempt deleted it
    assert not (project / "staged.txt").exists()  # the staged addition goes
    assert not (project / "untracked.txt").exists()  # the untracked work goes
    assert not (project / "late.txt").exists()
    assert not (project / "sub").exists()  # the new directory goes, emptied
    assert (project / "debug.log").exists()  # ignored and untracked: never touched
    assert (project / ".env").exists()
    assert (project / "ignored-tracked.txt").read_text(encoding="utf-8") == "tracked though ignored\n"  # tracked: restored, whatever the exclude rules say
    assert _status_outside_factory(project) == []  # the index restored to the base too
    assert git_in(project, "rev-parse", "HEAD").strip() == base  # HEAD never moves
    assert git_in(project, "for-each-ref", "--format=%(refname)",
                  "refs/orchestrator-iteration/", "refs/orchestrator-laid/").split() == []


def test_start_also_removes_the_task_s_disposition_record(tmp_path):
    project = _project(tmp_path)
    dispositions = project / ".ai-factory" / "dispositions"
    dispositions.mkdir()
    (dispositions / "01-1-1-door.json").write_text('{"an-entry": {"kind": "fixed"}}', encoding="utf-8")
    (dispositions / "99-another-task.json").write_text('{"another-entry": {"kind": "dismissed"}}', encoding="utf-8")

    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    # The record describes reviews the position discards, so it goes with
    # them; another task's record is not this task's to remove.
    assert not (dispositions / "01-1-1-door.json").exists()
    assert (dispositions / "99-another-task.json").read_text(encoding="utf-8") == '{"another-entry": {"kind": "dismissed"}}'


def test_start_takes_its_base_from_the_iteration_ref_s_parent(tmp_path):
    """The ref names the attempt's latest iteration commit, whose parent is
    the base the attempt began on: where no ref resolves the base is HEAD,
    and the two restore differently here."""
    # Without the ref: a commit after the base holds the attempt's staged
    # work, so HEAD is the base — what it holds stays, what it lacks goes.
    project = _project(tmp_path, iteration=False)
    git_in(project, "commit", "-q", "--allow-empty", "-m", "a commit after the base")

    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    # The unstaged edit of tracked.txt never reached the later commit, so the
    # base holds the base content of it; the staged addition the commit took
    # stays; the deletion it took stays; nothing untracked survives.
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert (project / "staged.txt").exists()  # the later commit holds it
    assert not (project / "gone-after.txt").exists()  # the later commit holds its deletion
    assert not (project / "untracked.txt").exists()  # no commit holds it
    assert not (project / "sub").exists()
    assert _factory_files(project) == {"ROADMAP.md"}
    assert _status_outside_factory(project) == []

    # With the ref: its parent — the original base — is the base, and the
    # attempt's whole work in the tree goes however far HEAD moved.
    project = _project(tmp_path / "twin")
    base = git_in(project, "rev-parse", "HEAD").strip()
    git_in(project, "commit", "-q", "--allow-empty", "-m", "a commit after the base")
    result = _rollback(tmp_path / "twin", "--project-dir", str(project), "1.1", "start")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert not (project / "staged.txt").exists()
    assert (project / "gone-after.txt").exists()  # restored: the attempt deleted it
    changed = [path for path in git_in(project, "diff", "--no-renames", "--name-only", base).splitlines()
               if not path.startswith(".ai-factory")]
    assert changed == []  # outside .ai-factory the tree is the base's again


def test_an_untracked_ignored_path_the_diff_reaches_is_never_touched(tmp_path):
    """A file the base tracks, the attempt untracked from the index and filled
    with local content: the index leg of the diff names it, but git's
    untracked-ignored listing names it too, so the restore leaves both the
    file and the staged untracking exactly as the attempt left them."""
    project = _project(tmp_path)
    git_in(project, "rm", "-q", "--cached", "ignored-tracked.txt")
    (project / "ignored-tracked.txt").write_text("local content, untracked and ignored\n", encoding="utf-8")

    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "start"\n}\n'
    assert (project / "ignored-tracked.txt").read_text(encoding="utf-8") == "local content, untracked and ignored\n"
    listed = git_in(project, "ls-files", "--", "ignored-tracked.txt").strip()
    assert listed == ""  # the staged untracking stands, the path never touched
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "base\n"  # the rest is the base's
    assert not (project / "untracked.txt").exists()


def test_a_ref_removal_warning_never_reaches_the_command_s_standard_output(tmp_path):
    """The ref removal's own warning goes to standard error for this command,
    so standard output holds the one JSON object and nothing else — an origin
    that cannot be reached leaves the rollback's answer whole."""
    project = _project(tmp_path)
    git_in(project, "remote", "set-url", "origin", str(tmp_path / "gone-origin.git"))

    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "start"\n}\n'
    assert "could not be removed, continuing anyway" in result.stderr.decode("utf-8")
    # The rollback finished: the group is gone however the remote answered.
    assert _factory_files(project) == {"ROADMAP.md"}
    assert git_in(project, "for-each-ref", "--format=%(refname)",
                  "refs/orchestrator-iteration/").split() == []


def test_an_untracked_nested_repository_is_removed_by_start(tmp_path):
    """A repository of its own under the project is one directory entry in
    git's untracked listing; the restore takes it whole as the untracked work
    it is, and no failure of the removal escapes as a traceback."""
    project = _project(tmp_path)
    nested = project / "nested"
    nested.mkdir()
    git_in(nested, "init", "-q", "-b", "main", ".")
    git_in(nested, "config", "user.email", "nested@example.com")
    git_in(nested, "config", "user.name", "Nested")
    (nested / "own.txt").write_text("a repository of its own\n", encoding="utf-8")
    git_in(nested, "add", "own.txt")
    git_in(nested, "commit", "-q", "-m", "its own work")

    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "start"\n}\n'
    assert not nested.exists()
    assert (project / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert _status_outside_factory(project) == []


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_a_position_the_task_cannot_take_refuses(tmp_path):
    project = _project(tmp_path, record=RECORD_ON)
    before = _state_of(project)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "implement")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == (
        b"Position 'implement' is not one task '1.1' can take: start, plan-review, verify\n"
    )
    assert _state_of(project) == before


def test_a_done_task_refuses(tmp_path):
    project = _project(tmp_path, done=True)
    before = _state_of(project)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"Task '1.1' is done: it takes no position\n"
    assert _state_of(project) == before


def test_a_task_with_no_attempt_refuses(tmp_path):
    project = _project(tmp_path)
    before = _state_of(project)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.2", "start")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"Task '1.2' has no attempt: there is nothing to roll back\n"
    assert _state_of(project) == before


def test_a_task_the_roadmap_does_not_hold_refuses(tmp_path):
    project = _project(tmp_path)
    result = _rollback(tmp_path, "--project-dir", str(project), "9.9", "start")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"No task '9.9' in the roadmap\n"


def test_a_missing_position_refuses_as_one_line(tmp_path):
    project = _project(tmp_path)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1")
    assert result.returncode == 1
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8")
    assert len(stderr.splitlines()) == 1
    assert "required: position" in stderr


def test_an_unknown_option_refuses_as_one_line(tmp_path):
    project = _project(tmp_path)
    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start", "--bogus")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"unrecognized arguments: --bogus\n"


# ---------------------------------------------------------------------------
# A crash after each step, the command repeated to the same end state
# ---------------------------------------------------------------------------


_STEPS = {
    "plan-review": ["_replace_sessions", "_remove_artifacts"],
    "verify": ["_replace_sessions", "_remove_artifacts"],
    "implement": ["_replace_sessions", "_remove_artifacts"],
    "start": ["_remove_artifacts", "_restore_tree", "remove_iteration_refs", "_delete_sidecar"],
}

# `implement` is a position of a planning-off record alone, which holds no
# planner; every other position runs against the planning-on record.
_RECORD_OF_POSITION = {
    "plan-review": RECORD_ON,
    "verify": RECORD_ON,
    "implement": RECORD_OFF,
    "start": RECORD_ON,
}


def _crash_after(monkeypatch: pytest.MonkeyPatch, steps: list[str], count: int) -> None:
    """Raise once `count` of the named steps have completed, as a command
    that died after its `count`-th step."""
    completed = {"n": 0}
    for name in steps:
        real = getattr(run_data, name)

        def wrapped(*args, __real=real, **kwargs):
            __real(*args, **kwargs)
            completed["n"] += 1
            if completed["n"] >= count:
                raise RuntimeError("injected crash")

        monkeypatch.setattr(run_data, name, wrapped)


@pytest.mark.parametrize("position, crash_at", [
    (position, step + 1)
    for position, steps in _STEPS.items()
    for step in range(len(steps))
])
def test_a_crash_after_each_step_is_finished_by_the_repeated_command(tmp_path, monkeypatch, position, crash_at):
    record = _RECORD_OF_POSITION[position]
    clean = _project(tmp_path / "clean", record=record)
    run_data.rollback(_config(), clean, task="1.1", position=position)
    end_state = _state_of(clean)

    crashed = _project(tmp_path / "crashed", record=record)
    with pytest.raises(RuntimeError, match="injected crash"):
        with pytest.MonkeyPatch.context() as mp:
            _crash_after(mp, _STEPS[position], crash_at)
            run_data.rollback(_config(), crashed, task="1.1", position=position)
    # The position is still listed for the crashed project, or the crash ate
    # the last step and the state already is the end state.
    assert position in _positions(crashed) or _state_of(crashed) == end_state
    try:
        run_data.rollback(_config(), crashed, task="1.1", position=position)
    except RunDataRefusal:
        pass  # nothing of the attempt remains to take a position
    assert _state_of(crashed) == end_state


def test_a_repeated_start_after_the_last_step_refuses_with_nothing_left(tmp_path):
    project = _project(tmp_path)
    run_data.rollback(_config(), project, task="1.1", position="start")
    end_state = _state_of(project)
    with pytest.raises(RunDataRefusal, match="has no attempt"):
        run_data.rollback(_config(), project, task="1.1", position="start")
    assert _state_of(project) == end_state

"""The `rollback` command against a real git repository: each position's
records and artifacts with the working tree left standing, the positions
`show` lists for each state, the refusals, and a crash injected after each
step with the command repeated to the same end state."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import run_data
from orchestrator.config import OrchestratorConfig
from orchestrator.errors import RunDataRefusal
from orchestrator.iteration_ref import make_iteration_commit, push_iteration_ref
from tests.conftest import git_environment, git_in

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
    carries and git's configuration files silenced, so the command reads no
    config of the machine it runs on."""
    environment = git_environment()
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


def _positions(project: Path, address: str = "1.1", relpath: str = "ROADMAP.md") -> list[str]:
    task = _task_of(project, address)
    return run_data.positions_of(project, relpath, task, run_data.locate_task(project, relpath, task))


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


@pytest.mark.parametrize("step", ["implemented:1", "implemented:2", "review_failed:1", "test_run_failed:2"])
def test_a_record_whose_step_shows_an_implementation_lists_verify(tmp_path, step):
    """`verify` is listed from the records alone: the record's step shows an
    implementation — the implementer finished an iteration, or the verify
    that read it failed and waits to run again. The implementer's answer is
    removed, so the step alone decides."""
    project = _project(tmp_path, record=dict(RECORD_ON, step=step))
    (project / ".ai-factory" / "reviews" / "01-1-1-door-implementer-1.md").unlink()
    assert _positions(project) == ["start", "plan-review", "verify"]


def test_an_implementer_s_answer_that_stands_lists_verify(tmp_path):
    """However the record's step reads, an implementer's answer standing
    among the group's artifacts is an implementation the attempt made."""
    project = _project(tmp_path, record=dict(RECORD_ON, step="escalated"))
    assert _positions(project) == ["start", "plan-review", "verify"]


def test_a_named_roadmap_s_answer_alone_lists_verify(tmp_path):
    """The implementer's answer is read in the roadmap's own artifact
    segment, wherever under `.ai-factory` the roadmap lies: a group whose
    record shows no implementation still lists `verify` through its answer
    alone."""
    from orchestrator.roadmap import find_task, parse_roadmap

    project = tmp_path / "project"
    factory = project / ".ai-factory"
    (factory / "roadmaps").mkdir(parents=True)
    (factory / "roadmaps" / "doors.md").write_text(
        "# Named roadmap\n\n## Phase 7 — side doors\n\n- [ ] **7.1 — Eta** — the task under a named roadmap\n",
        encoding="utf-8",
    )
    (factory / "plans" / "doors").mkdir(parents=True)
    (factory / "reviews" / "doors").mkdir(parents=True)
    (factory / "plans" / "doors" / "01-7-1-eta.md").write_text("# Plan: 7.1 — Eta\n\nThe plan.\n", encoding="utf-8")
    (factory / "plans" / "doors" / "01-7-1-eta.json").write_text(
        json.dumps({"planner": "plan-session", "step": "escalated"}, indent=2), encoding="utf-8",
    )
    (factory / "reviews" / "doors" / "01-7-1-eta-implementer-1.md").write_text("Implemented Eta.\n", encoding="utf-8")

    task = find_task(parse_roadmap(factory / "roadmaps" / "doors.md").tasks, "7.1")
    group = run_data.locate_task(project, "roadmaps/doors.md", task)
    assert run_data.positions_of(project, "roadmaps/doors.md", task, group) == ["start", "plan-review", "verify"]


def test_a_record_that_shows_no_implementation_lists_no_verify_whatever_the_tree_holds(tmp_path):
    """The working tree is never read: a change outside `.ai-factory`,
    however plain, is no word of where the attempt stands."""
    project = _project(tmp_path, record=dict(RECORD_ON, step="planned:1"))
    (project / ".ai-factory" / "reviews" / "01-1-1-door-implementer-1.md").unlink()
    assert _positions(project) == ["start", "plan-review"]


def test_a_clean_tree_still_lists_verify_where_the_record_shows_an_implementation(tmp_path):
    project = _project(tmp_path, tree_change=False, iteration=False)
    assert _positions(project) == ["start", "plan-review", "verify"]


def test_a_task_rolled_back_to_verify_still_lists_it(tmp_path):
    """The rollback leaves the record's step at `implemented:1`, so the
    position stays listed for the task it just rolled back."""
    project = _project(tmp_path)
    run_data.rollback(_config(), project, task="1.1", position="verify")
    assert _positions(project) == ["start", "plan-review", "verify"]


def test_a_project_git_cannot_read_still_lists_verify_from_the_records(tmp_path):
    project = _project(tmp_path, tree_change=False, iteration=False)
    git_in(project, "rev-parse", "--verify", "--quiet", "HEAD")  # a repository, then not one
    (project / ".git").rename(tmp_path / "set-aside.git")
    (project / "work.txt").write_text("a change with no repository to read it\n")
    assert _positions(project) == ["start", "plan-review", "verify"]


def test_a_planning_off_task_whose_attempt_implemented_lists_verify_beside_implement(tmp_path):
    project = _project(tmp_path, record=RECORD_OFF, tree_change=False, iteration=False)
    assert _positions(project) == ["start", "verify", "implement"]


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


def test_start_leaves_a_tracked_change_an_untracked_file_and_an_ignored_file_as_they_were(tmp_path):
    """`rollback` resets the orchestrator's own records and touches nothing
    else, the working tree and the index included: what becomes of an
    attempt's code, kept, repaired or discarded, is the head's own act with
    git, never the command's."""
    project = _project(tmp_path)
    files = ("tracked.txt", "staged.txt", "late.txt", "untracked.txt",
             "sub/work.txt", "debug.log", ".env", "ignored-tracked.txt")
    before = {name: (project / name).read_bytes() for name in files}
    before_status = _status_outside_factory(project)
    before_head = git_in(project, "rev-parse", "HEAD").strip()

    result = _rollback(tmp_path, "--project-dir", str(project), "1.1", "start")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == b'{\n  "version": 1,\n  "position": "start"\n}\n'
    # A tracked change, an untracked file and an ignored file stand as they
    # were, byte for byte — the staged addition and the attempt's work in a
    # new directory no less.
    for name in files:
        assert (project / name).read_bytes() == before[name]
    assert not (project / "gone-after.txt").exists()  # the attempt's deletion stands
    assert _status_outside_factory(project) == before_status  # the index untouched too
    assert git_in(project, "rev-parse", "HEAD").strip() == before_head  # HEAD never moves
    # The attempt's own state is gone: its artifacts, its record, its refs.
    assert _factory_files(project) == {"ROADMAP.md"}
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
    "start": ["_remove_artifacts", "remove_iteration_refs", "_delete_sidecar"],
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

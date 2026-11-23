"""The implementer's result: his pushed branch laid as the run's working tree, against real git."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import git_runner
from orchestrator.agents import Implementer
from orchestrator.errors import (
    BranchNotFetchedError,
    BranchNotFromBaseError,
    EscalationError,
    LeftoverBranchNotRemovedError,
)
from orchestrator.implementer_result import HermesResult, WorkingTreeResult, branch_of
from tests.conftest import git_in

ESCALATION_ARTIFACT = "Some notes\n\n## Escalation\nMissing decision: pick A or B.\n\nESCALATION"
BRANCH = "orchestrator/01-slug"
REF = f"refs/remotes/origin/{BRANCH}"
LAID = "refs/orchestrator-laid/01-slug"

def _heads(origin: Path, branch: str) -> str:
    return subprocess.run(
        ["git", "ls-remote", "--heads", str(origin), f"refs/heads/{branch}"],
        capture_output=True, text=True,
    ).stdout.strip()


def _remote_ref(origin: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "ls-remote", str(origin), ref],
        capture_output=True, text=True,
    ).stdout.strip()


def _ref_held(cwd: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=str(cwd),
        capture_output=True, text=True,
    ).returncode == 0


@pytest.fixture
def stand(tmp_path, request):
    """A bare origin, the run's clone at the base, and his clone for building branches."""
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(origin))
    run = tmp_path / "run"
    single_branch = getattr(request, "param", False)
    if single_branch:
        git_in(tmp_path, "clone", "--single-branch", str(origin), str(run))
    else:
        git_in(tmp_path, "clone", str(origin), str(run))
    (run / "a.txt").write_text("base\n")
    (run / "b.txt").write_text("base b\n")
    (run / "[ab].txt").write_text("bracketed base\n")
    (run / "y").write_text("a file\n")
    (run / "d").mkdir()
    (run / "d" / "x.txt").write_text("a directory's content\n")
    (run / "sub").mkdir()
    (run / "sub" / "s.txt").write_text("base sub\n")
    (run / "work").mkdir()
    (run / "work" / "inner.txt").write_text("base inner\n")
    (run / "outside.txt").write_text("base outside\n")
    run_sh = run / "run.sh"
    run_sh.write_text("#!/bin/sh\nexit 0\n")
    run_sh.chmod(0o755)
    (run / "mode.sh").write_text("#!/bin/sh\necho mode\n")
    factory = run / ".ai-factory"
    (factory / "plans").mkdir(parents=True)
    (factory / "plans" / "01-slug.md").write_text("# Plan\n")
    (factory / "plans" / "01-slug.json").write_text("{}")
    (factory / "ARCHITECTURE.md").write_text("# Architecture\n")
    git_in(run, "add", "-A")
    git_in(run, "commit", "-m", "the beginning")
    (run / "b.txt").write_text("the run's work\n")
    git_in(run, "add", "-A")
    git_in(run, "commit", "-m", "the base")
    base = git_in(run, "rev-parse", "HEAD").strip()
    git_in(run, "push", "origin", f"{base}:refs/heads/main")
    his = tmp_path / "his"
    git_in(tmp_path, "clone", str(origin), str(his))
    return SimpleNamespace(origin=origin, run=run, his=his, base=base, factory=factory)


def _push_his_branch(stand, change):
    """Build and push his branch from the base, applying `change` to his clone."""
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, stand.base)
    change(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "his work")
    git_in(stand.his, "push", "origin", BRANCH)


def _result(stand, mode_dirs=("plans", "plan-reviews", "reviews", "test-runs", "dispositions")):
    preserved = tuple(stand.factory / name for name in mode_dirs)
    return HermesResult(stand.run, preserved)


def test_the_working_tree_result_does_nothing_and_says_nothing(stand):
    plan = stand.factory / "plans" / "01-slug.md"
    git_in(stand.his, "push", str(stand.origin), f"{stand.base}:refs/heads/{BRANCH}")
    before = (stand.run / "a.txt").read_text()

    WorkingTreeResult().prepare(plan)
    WorkingTreeResult().lay(plan)
    assert WorkingTreeResult().channel_text(plan, first=True) == ""
    assert WorkingTreeResult().channel_text(plan, first=False) == ""

    assert (stand.run / "a.txt").read_text() == before
    assert _heads(stand.origin, BRANCH) != ""  # a leftover is not the run-tree implementer's to remove


def test_the_channel_names_the_branch_and_how_to_reach_it(stand):
    plan = stand.factory / "plans" / "01-slug.md"
    result = _result(stand)

    first = result.channel_text(plan, first=True)
    assert BRANCH in first and "base commit" in first and "push it to origin" in first
    continued = result.channel_text(plan, first=False)
    assert BRANCH in continued and "Continue the branch" in continued and "base commit" not in continued


ITERATION_REF = "refs/orchestrator-iteration/01-slug"  # an iteration ref origin holds in the run's namespace


def test_prepare_removes_a_leftover_branch_of_an_earlier_attempt(stand):
    git_in(stand.his, "push", str(stand.origin), f"{stand.base}:refs/heads/{BRANCH}")
    assert _heads(stand.origin, BRANCH)
    git_in(stand.run, "update-ref", LAID, stand.base)  # an earlier attempt's lay left the laid ref
    assert _ref_held(stand.run, LAID)
    git_in(stand.his, "push", str(stand.origin), f"{stand.base}:{ITERATION_REF}")  # the run's iteration ref sits beside it
    assert _remote_ref(stand.origin, ITERATION_REF)

    _result(stand).prepare(stand.factory / "plans" / "01-slug.md")

    assert _heads(stand.origin, BRANCH) == ""
    assert _remote_ref(stand.origin, ITERATION_REF)  # the run's iteration ref is not the branch
    assert not _ref_held(stand.run, LAID)  # and its tip is not read as this task's previous lay


def test_prepare_leaves_a_remote_that_holds_no_leftover_alone(stand):
    _result(stand).prepare(stand.factory / "plans" / "01-slug.md")

    assert _heads(stand.origin, BRANCH) == ""


def test_a_refused_removal_halts_naming_the_branch_and_git_s_text(stand, monkeypatch):
    hook = stand.origin / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        "  if [ \"$new\" = \"0000000000000000000000000000000000000000\" ]; then\n"
        "    echo 'branch is protected' >&2\n"
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)
    git_in(stand.his, "push", str(stand.origin), f"{stand.base}:refs/heads/{BRANCH}")
    assert _heads(stand.origin, BRANCH)

    with pytest.raises(LeftoverBranchNotRemovedError) as caught:
        _result(stand).prepare(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "A branch left on the remote could not be removed"
    assert lines[1] == BRANCH
    assert "branch is protected" in str(caught.value)


def test_a_failed_lookup_of_the_leftover_halts(stand, tmp_path):
    git_in(stand.run, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))

    with pytest.raises(LeftoverBranchNotRemovedError) as caught:
        _result(stand).prepare(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "A leftover branch could not be looked up on the remote"
    assert lines[1] == BRANCH
    assert lines[2]


def test_the_lay_changes_exactly_the_paths_his_work_changed(stand):
    def change(his):
        (his / "a.txt").write_text("his version\n")
        (his / "sub" / "s.txt").unlink()
        new = his / "new" / "dir" / "n.sh"
        new.parent.mkdir(parents=True)
        new.write_text("#!/bin/sh\nexit 3\n")
        new.chmod(0o755)
        (his / "mode.sh").chmod(0o755)  # a mode change of a file his work never rewrites
        (his / ".ai-factory" / "ARCHITECTURE.md").write_text("# His architecture\n")
        (his / ".ai-factory" / "plans" / "01-slug.md").write_text("# His plan\n")
    _push_his_branch(stand, change)
    # The run's own artifacts move during the task, an untracked file lies around,
    # and a tracked file outside his changes carries an uncommitted edit.
    reviews = stand.factory / "reviews"
    reviews.mkdir()
    (reviews / "01-slug-review-1.md").write_text("the run's review\n")
    (stand.factory / "plans" / "01-slug.json").write_text(json.dumps({"step": "implemented:1"}))
    (stand.run / "leftover.txt").write_text("the run's untracked file\n")
    (stand.run / "run.sh").write_text("#!/bin/sh\nexit 1\n")  # uncommitted, and his work never touches it
    (stand.run / "a.txt").write_text("the run's own edit\n")  # uncommitted, on a path he changed

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "his version\n"  # his version over the run's edit
    assert not (stand.run / "sub" / "s.txt").exists()  # his deletion
    assert (stand.run / "new" / "dir" / "n.sh").read_text() == "#!/bin/sh\nexit 3\n"
    assert (stand.run / "new" / "dir" / "n.sh").stat().st_mode & 0o111
    assert (stand.run / "mode.sh").stat().st_mode & 0o111  # his mode change
    assert (stand.run / "run.sh").read_text() == "#!/bin/sh\nexit 1\n"  # the run's uncommitted edit kept
    assert (stand.run / "b.txt").read_text() == "the run's work\n"  # untouched paths kept as they are
    assert (stand.factory / "ARCHITECTURE.md").read_text() == "# His architecture\n"  # his edit lands
    assert (stand.factory / "plans" / "01-slug.md").read_text() == "# Plan\n"  # artifact paths never touched
    assert json.loads((stand.factory / "plans" / "01-slug.json").read_text()) == {"step": "implemented:1"}
    assert (reviews / "01-slug-review-1.md").read_text() == "the run's review\n"
    assert (stand.run / "leftover.txt").read_text() == "the run's untracked file\n"
    assert git_in(stand.run, "rev-parse", "HEAD").strip() == stand.base
    status = git_in(stand.run, "status", "--porcelain")
    assert "M  a.txt" in status and "D  sub/s.txt" in status and "A  new/dir/n.sh" in status
    assert "M  mode.sh" in status and " M run.sh" in status


def test_a_lay_of_his_changes_and_deletions_of_records_leaves_the_local_records(stand):
    """A task's disposition record is the run's own artifact, preserved like the
    plans and the reviews: a change, an addition or a deletion of a path beneath
    the dispositions directory never lays, so the local records stand exactly as
    the run wrote them."""
    records = stand.factory / "dispositions"
    records.mkdir()
    (records / "00-earlier.json").write_text('{"earlier": "the base"}\n')
    (records / "00-gone.json").write_text('{"gone": "the base"}\n')
    git_in(stand.run, "add", ".ai-factory/dispositions")
    git_in(stand.run, "commit", "-m", "earlier records")
    stand.base = git_in(stand.run, "rev-parse", "HEAD").strip()
    git_in(stand.run, "push", "origin", f"{stand.base}:refs/heads/main")
    git_in(stand.his, "fetch", "-q", "origin")
    # the run's own state during the task: an edited earlier record, and the task's record the run wrote
    (records / "00-earlier.json").write_text('{"earlier": "the run\'s edit"}\n')
    (records / "01-slug.json").write_text('{"task": "the run\'s record"}\n')

    def change(his):
        (his / "a.txt").write_text("his version\n")  # outside the artifact paths, so the lay runs
        (his / ".ai-factory" / "dispositions" / "00-earlier.json").write_text('{"earlier": "his version"}\n')
        git_in(his, "rm", "-q", ".ai-factory/dispositions/00-gone.json")
        (his / ".ai-factory" / "dispositions" / "01-slug.json").write_text('{"task": "his version"}\n')
    _push_his_branch(stand, change)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "his version\n"  # his work outside the artifacts lays
    assert (records / "00-earlier.json").read_text() == '{"earlier": "the run\'s edit"}\n'  # his change never lays
    assert (records / "00-gone.json").read_text() == '{"gone": "the base"}\n'  # his deletion never lays
    assert (records / "01-slug.json").read_text() == '{"task": "the run\'s record"}\n'  # his addition never lays
    assert git_in(stand.run, "rev-parse", "HEAD").strip() == stand.base


def test_a_fix_iteration_s_lay_reverts_his_first_changes_through_the_previous_tip(stand):
    def first(his):
        (his / "a.txt").write_text("first iteration\n")
        (his / "sub" / "s.txt").unlink()
    _push_his_branch(stand, first)
    assert not _ref_held(stand.run, LAID)  # a first lay with no laid ref lays from the base
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")
    assert (stand.run / "a.txt").read_text() == "first iteration\n"
    assert _ref_held(stand.run, LAID)  # the first lay's advance left the ref the second lay reads

    def second(his):
        (his / "a.txt").write_text("second iteration\n")
        git_in(his, "checkout", stand.base, "--", "sub/s.txt")
    second(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "the fixes")
    git_in(stand.his, "push", "origin", BRANCH)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "second iteration\n"
    assert (stand.run / "sub" / "s.txt").read_text() == "base sub\n"  # returned to the base's content
    assert git_in(stand.run, "rev-parse", "HEAD").strip() == stand.base


def test_a_lay_that_fails_keeps_the_previous_tip(stand):
    def first(his):
        (his / "a.txt").write_text("first iteration\n")
    _push_his_branch(stand, first)
    first_tip = git_in(stand.his, "rev-parse", BRANCH).strip()
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")  # iteration 1's tip is laid
    assert (stand.run / "a.txt").read_text() == "first iteration\n"
    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip

    def second(his):  # iteration 2 returns a.txt to the base's text, on top of iteration 1
        (his / "a.txt").write_text("base\n")
    second(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "the fix returns his change")
    second_tip = git_in(stand.his, "rev-parse", BRANCH).strip()
    git_in(stand.his, "push", "origin", BRANCH)

    lock = stand.run / ".git" / "index.lock"
    lock.write_text("")
    with pytest.raises(RuntimeError) as caught:
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")
    assert str(caught.value).splitlines()[0] == "Hermes implementer branch could not be laid as the working tree"
    assert (stand.run / "a.txt").read_text() == "first iteration\n"
    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip  # the failed lay kept it
    lock.unlink()

    # The resumed call's base check fetches origin, which moves the tracking ref —
    # a fetch is no writer of the laid ref, so the retry still sees iteration 1.
    git_in(stand.run, "fetch", "origin")
    assert git_in(stand.run, "rev-parse", REF).strip() == second_tip

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "base\n"  # restored even after the fetch
    assert git_in(stand.run, "rev-parse", LAID).strip() == second_tip


def test_a_lay_that_halts_as_not_from_the_base_leaves_the_laid_ref_where_it_was(stand):
    def first(his):
        (his / "a.txt").write_text("first iteration\n")
    _push_his_branch(stand, first)
    first_tip = git_in(stand.his, "rev-parse", BRANCH).strip()
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")
    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip

    older = git_in(stand.his, "rev-parse", f"{stand.base}^").strip()
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, older)
    (stand.his / "a.txt").write_text("cut from an older commit\n")
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "his older work")
    git_in(stand.his, "push", "--force", "origin", BRANCH)

    with pytest.raises(BranchNotFromBaseError):
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip  # kept where the last lay left it


def test_a_lay_advances_the_laid_ref_and_writes_no_tracking_ref(stand):
    def change(his):
        (his / "a.txt").write_text("his version\n")
    _push_his_branch(stand, change)
    his_tip = git_in(stand.his, "rev-parse", BRANCH).strip()

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert git_in(stand.run, "rev-parse", LAID).strip() == his_tip  # the fetched tip
    assert not _ref_held(stand.run, REF)  # the lay's fetch wrote no tracking ref


def test_a_tip_that_does_not_descend_from_the_base_halts(stand):
    older = git_in(stand.his, "rev-parse", f"{stand.base}^").strip()
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, older)
    (stand.his / "a.txt").write_text("cut from an older commit\n")
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "his older work")
    git_in(stand.his, "push", "origin", BRANCH)

    with pytest.raises(BranchNotFromBaseError) as caught:
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The implementer's branch does not descend from the base"
    assert lines[1] == BRANCH
    assert lines[2] == stand.base
    # Nothing is laid: the run's tree and index stand exactly as they were.
    assert (stand.run / "a.txt").read_text() == "base\n"
    assert (stand.run / "b.txt").read_text() == "the run's work\n"
    assert git_in(stand.run, "status", "--porcelain").strip() == ""

    def stacked(his):  # a tip stacked on the base passes
        (his / "a.txt").write_text("stacked on the base\n")
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, stand.base)
    stacked(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "stacked on the base")
    git_in(stand.his, "push", "--force", "origin", BRANCH)  # the rejected older tip sits on the remote

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "stacked on the base\n"


def test_a_lay_of_a_branch_origin_does_not_hold_halts(stand):
    with pytest.raises(BranchNotFetchedError) as caught:
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The implementer's branch could not be fetched"
    assert lines[1] == BRANCH
    assert lines[2]
    assert (stand.run / "a.txt").read_text() == "base\n"
    assert git_in(stand.run, "status", "--porcelain").strip() == ""


class _SilentOrigin:
    """A local endpoint that accepts connections and never answers, as a remote
    that hangs every git call sent to it."""

    def __init__(self):
        import socket
        import threading

        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(5)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return

    def close(self):
        self.socket.close()


def test_a_fetch_that_never_answers_halts_as_the_fetch_timeout_fault(stand, monkeypatch):
    silent = _SilentOrigin()
    try:
        monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
        git_in(stand.run, "remote", "set-url", "origin", f"http://127.0.0.1:{silent.port}/origin.git")

        with pytest.raises(BranchNotFetchedError) as caught:
            _result(stand).lay(stand.factory / "plans" / "01-slug.md")

        lines = str(caught.value).splitlines()
        assert lines[0] == "The implementer's branch could not be fetched"
        assert lines[1] == BRANCH
        assert lines[2].startswith("timed out after")

        # A timeout of any other git call surfaces as the TimeoutExpired it is.
        with pytest.raises(subprocess.TimeoutExpired):
            _result(stand).prepare(stand.factory / "plans" / "01-slug.md")
    finally:
        silent.close()


@pytest.mark.parametrize("renames", [None, "copies"])
def test_a_pure_move_is_its_deletion_and_its_addition(stand, renames):
    if renames is not None:
        git_in(stand.run, "config", "diff.renames", renames)

    def move(his):
        git_in(his, "mv", "a.txt", "moved.txt")
    _push_his_branch(stand, move)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert not (stand.run / "a.txt").exists()  # the source is removed
    assert (stand.run / "moved.txt").read_text() == "base\n"  # the destination is written


def test_a_move_into_the_artifact_paths_removes_the_source_and_writes_no_destination(stand):
    def move(his):
        git_in(his, "mv", ".ai-factory/ARCHITECTURE.md", ".ai-factory/plans/moved-architecture.md")
    _push_his_branch(stand, move)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert not (stand.factory / "ARCHITECTURE.md").exists()
    assert not (stand.factory / "plans" / "moved-architecture.md").exists()


@pytest.mark.parametrize("deletes", [False, True])
def test_bracketed_names_are_laid_as_themselves(stand, deletes):
    def change(his):
        if deletes:
            git_in(his, "--literal-pathspecs", "rm", "-q", "[ab].txt")  # the colliding name, as a literal path
        else:
            (his / "[ab].txt").write_text("his bracketed version\n")
    _push_his_branch(stand, change)
    (stand.run / "a.txt").write_text("the run's uncommitted edit\n")  # dirty, and his work never touches it

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "the run's uncommitted edit\n"  # no pattern matched it
    assert (stand.run / "b.txt").read_text() == "the run's work\n"  # untouched, brackets or no brackets
    if deletes:
        assert not (stand.run / "[ab].txt").exists()
    else:
        assert (stand.run / "[ab].txt").read_text() == "his bracketed version\n"


def test_a_project_in_a_subdirectory_of_its_repository_is_laid(stand):
    def change(his):
        (his / "work" / "inner.txt").write_text("his edit inside the project\n")
        (his / "outside.txt").write_text("his edit outside the project\n")
    _push_his_branch(stand, change)
    project = stand.run / "work"
    (project / ".ai-factory" / "plans").mkdir(parents=True)
    preserved = tuple(project / ".ai-factory" / name for name in ("plans", "plan-reviews", "reviews", "test-runs"))
    result = HermesResult(project, preserved)

    result.lay(project / ".ai-factory" / "plans" / "01-slug.md")

    assert (project / "inner.txt").read_text() == "his edit inside the project\n"
    assert (stand.run / "outside.txt").read_text() == "base outside\n"  # outside the project, never laid
    assert git_in(stand.run, "rev-parse", "HEAD").strip() == stand.base


@pytest.mark.parametrize("stand", [True], indirect=True)
def test_a_single_branch_clone_still_reads_the_previous_tip(stand):
    def first(his):
        (his / "a.txt").write_text("first iteration\n")
        git_in(his, "rm", "-q", "sub/s.txt")
    _push_his_branch(stand, first)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "first iteration\n"
    assert _ref_held(stand.run, LAID)  # the lay advanced its own ref, whatever the clone's refspec is

    def second(his):
        git_in(his, "checkout", stand.base, "--", "sub/s.txt")
    second(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "the fixes")
    git_in(stand.his, "push", "origin", BRANCH)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "sub" / "s.txt").read_text() == "base sub\n"  # returned through the previous tip


def test_a_held_lock_on_the_laid_ref_halts_the_preparation(stand):
    git_in(stand.run, "update-ref", LAID, stand.base)
    assert _ref_held(stand.run, LAID)
    lock = stand.run / ".git" / "refs" / "orchestrator-laid" / "01-slug.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")

    with pytest.raises(LeftoverBranchNotRemovedError) as caught:
        _result(stand).prepare(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "A leftover branch's laid tip could not be removed"
    assert lines[1] == LAID
    assert lines[2]
    lock.unlink()
    _result(stand).prepare(stand.factory / "plans" / "01-slug.md")  # a preparation with no ref halts nowhere


def test_a_file_and_a_directory_that_trade_places_lay(stand):
    def change(his):
        (his / "y").unlink()  # a file replaced by a directory of its name
        (his / "y").mkdir()
        (his / "y" / "z.txt").write_text("the tip's directory content\n")
        git_in(his, "rm", "-q", "-r", "d")  # a directory replaced by a file of its name
        (his / "d").write_text("the tip's file content\n")
    _push_his_branch(stand, change)

    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "y" / "z.txt").read_text() == "the tip's directory content\n"
    assert not (stand.run / "y").is_file()
    assert (stand.run / "d").read_text() == "the tip's file content\n"
    assert (stand.run / "d").is_file()


def test_implement_prepares_sends_the_channel_and_lays_his_branch(stand, monkeypatch):
    # His clone builds the branch locally; a lost earlier attempt's branch sits on the remote.
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, stand.base)
    (stand.his / "a.txt").write_text("his implemented work\n")
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "his work")
    git_in(stand.his, "push", str(stand.origin), f"{stand.base}:refs/heads/{BRANCH}")
    assert _heads(stand.origin, BRANCH) != ""

    observed = {}

    def on_call(call):
        observed["branch_at_call_time"] = _heads(stand.origin, BRANCH)
        git_in(stand.his, "push", "origin", BRANCH)  # his turn pushes his branch
        return "Did the work."

    backend = FakeRecorder(on_call)
    result = _result(stand)
    implementer = Implementer(stand.run, backend, result=result)
    plan = stand.factory / "plans" / "01-slug.md"
    answer = stand.run / ".ai-factory" / "reviews" / "01-slug-implementer-1.md"

    implementer.implement(plan, answer)

    assert observed["branch_at_call_time"] == ""
    (call,) = backend.calls
    assert call.prompt.endswith(
        f"Create the branch {BRANCH} from the base commit, commit your work to it, and push it to origin."
    )
    assert "Implement the plan below." in call.prompt
    assert answer.read_text() == "Did the work."
    assert (stand.run / "a.txt").read_text() == "his implemented work\n"
    assert json.loads(plan.with_suffix(".json").read_text())["implementer"]


def test_a_continued_session_skips_the_preparation_and_continues_the_branch(stand):
    def change(his):
        (his / "a.txt").write_text("his first work\n")
    _push_his_branch(stand, change)

    observed = {}

    def on_call(call):
        observed["branch_at_call_time"] = _heads(stand.origin, BRANCH)
        return "The fixes are in."

    backend = FakeRecorder(on_call)
    result = _result(stand)
    implementer = Implementer(stand.run, backend, result=result)
    implementer.session_id = "his-session"
    plan = stand.factory / "plans" / "01-slug.md"

    implementer.implement(plan, stand.run / ".ai-factory" / "reviews" / "01-slug-implementer-2.md")

    (call,) = backend.calls
    assert call.prompt.endswith(
        f"Continue the branch {BRANCH}, commit your work to it, and push it to origin."
    )
    assert observed["branch_at_call_time"] != ""  # the preparation never removed it


def test_an_escalated_answer_writes_and_raises_without_laying(stand):
    def change(his):
        (his / "a.txt").write_text("work that must not land\n")
    _push_his_branch(stand, change)
    backend = FakeRecorder(lambda call: ESCALATION_ARTIFACT)
    implementer = Implementer(stand.run, backend, result=_result(stand))
    plan = stand.factory / "plans" / "01-slug.md"

    with pytest.raises(EscalationError):
        implementer.implement(plan, stand.run / ".ai-factory" / "reviews" / "01-slug-implementer-1.md")

    assert (stand.run / "a.txt").read_text() == "base\n"


class FakeRecorder:
    """A fake backend recording calls, answering what `on_call` returns."""

    def __init__(self, on_call):
        self.on_call = on_call
        self.calls = []

    def run(self, *, role, prompt, cwd, instructions, session_id=None):
        self.calls.append(SimpleNamespace(role=role, prompt=prompt, cwd=cwd, instructions=instructions, session_id=session_id))
        result = self.on_call(self.calls[-1]) if self.on_call is not None else None
        return (result if isinstance(result, str) else "fake output"), "his-session"


def test_the_branch_name_is_the_plan_s_own_stem(stand):
    assert branch_of(stand.run, stand.factory / "plans" / "07-other-task.md") == "orchestrator/07-other-task"
    assert (
        branch_of(stand.run, stand.factory / "plans" / "john-doe" / "07-other-task.md")
        == "orchestrator/john-doe/07-other-task"
    )

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
from orchestrator.ports import Repository
from orchestrator.repositories import find_run_tree
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
    return HermesResult(stand.run, (Repository(directory=stand.run, folder=None),), preserved)


def _seed_bases(stand, *bases):
    """Write the task's record holding its bases, as the run writes it at the
    task's start: one commit per repository of the tree, in its order."""
    stand.factory.joinpath("plans", "01-slug.json").write_text(json.dumps({"bases": list(bases)}))


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
    assert BRANCH in continued and "continue the branch" in continued and "base commit" not in continued


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
    (stand.factory / "plans" / "01-slug.json").write_text(json.dumps({"step": "implemented:1", "bases": [stand.base]}))
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
    assert json.loads((stand.factory / "plans" / "01-slug.json").read_text()) == {"step": "implemented:1", "bases": [stand.base]}
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

    _seed_bases(stand, stand.base)
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
    _seed_bases(stand, stand.base)
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

    _seed_bases(stand, stand.base)
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "second iteration\n"
    assert (stand.run / "sub" / "s.txt").read_text() == "base sub\n"  # returned to the base's content
    assert git_in(stand.run, "rev-parse", "HEAD").strip() == stand.base


def test_a_lay_that_fails_keeps_the_previous_tip(stand):
    def first(his):
        (his / "a.txt").write_text("first iteration\n")
    _push_his_branch(stand, first)
    first_tip = git_in(stand.his, "rev-parse", BRANCH).strip()
    _seed_bases(stand, stand.base)
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
        _seed_bases(stand, stand.base)
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")
    assert str(caught.value).splitlines()[0] == "Hermes implementer branch could not be laid as the working tree"
    assert (stand.run / "a.txt").read_text() == "first iteration\n"
    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip  # the failed lay kept it
    lock.unlink()

    # The resumed call's base check fetches origin, which moves the tracking ref —
    # a fetch is no writer of the laid ref, so the retry still sees iteration 1.
    git_in(stand.run, "fetch", "origin")
    assert git_in(stand.run, "rev-parse", REF).strip() == second_tip

    _seed_bases(stand, stand.base)
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "base\n"  # restored even after the fetch
    assert git_in(stand.run, "rev-parse", LAID).strip() == second_tip


def test_a_lay_that_halts_as_not_from_the_base_leaves_the_laid_ref_where_it_was(stand):
    def first(his):
        (his / "a.txt").write_text("first iteration\n")
    _push_his_branch(stand, first)
    first_tip = git_in(stand.his, "rev-parse", BRANCH).strip()
    _seed_bases(stand, stand.base)
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")
    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip

    older = git_in(stand.his, "rev-parse", f"{stand.base}^").strip()
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, older)
    (stand.his / "a.txt").write_text("cut from an older commit\n")
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "his older work")
    git_in(stand.his, "push", "--force", "origin", BRANCH)

    with pytest.raises(BranchNotFromBaseError):
        _seed_bases(stand, stand.base)
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert git_in(stand.run, "rev-parse", LAID).strip() == first_tip  # kept where the last lay left it


def test_a_lay_advances_the_laid_ref_and_writes_no_tracking_ref(stand):
    def change(his):
        (his / "a.txt").write_text("his version\n")
    _push_his_branch(stand, change)
    his_tip = git_in(stand.his, "rev-parse", BRANCH).strip()

    _seed_bases(stand, stand.base)
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

    _seed_bases(stand, stand.base)
    with pytest.raises(BranchNotFromBaseError) as caught:
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The implementer's branch does not descend from the base"
    assert lines[1] == BRANCH
    assert lines[2] == stand.base
    # Nothing is laid: the run's tree and index stand exactly as they were,
    # the task's own record included.
    assert (stand.run / "a.txt").read_text() == "base\n"
    assert (stand.run / "b.txt").read_text() == "the run's work\n"
    assert git_in(stand.run, "status", "--porcelain").splitlines() == [" M .ai-factory/plans/01-slug.json"]

    def stacked(his):  # a tip stacked on the base passes
        (his / "a.txt").write_text("stacked on the base\n")
    git_in(stand.his, "checkout", "-q", "-B", BRANCH, stand.base)
    stacked(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "stacked on the base")
    git_in(stand.his, "push", "--force", "origin", BRANCH)  # the rejected older tip sits on the remote

    _seed_bases(stand, stand.base)
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "stacked on the base\n"


def test_a_lay_of_a_branch_origin_does_not_hold_halts(stand):
    _seed_bases(stand, stand.base)
    with pytest.raises(BranchNotFetchedError) as caught:
        _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The implementer's branch could not be fetched"
    assert lines[1] == BRANCH
    assert lines[2]
    assert (stand.run / "a.txt").read_text() == "base\n"
    assert git_in(stand.run, "status", "--porcelain").splitlines() == [" M .ai-factory/plans/01-slug.json"]


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
            _seed_bases(stand, stand.base)
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

    _seed_bases(stand, stand.base)
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert not (stand.run / "a.txt").exists()  # the source is removed
    assert (stand.run / "moved.txt").read_text() == "base\n"  # the destination is written


def test_a_move_into_the_artifact_paths_removes_the_source_and_writes_no_destination(stand):
    def move(his):
        git_in(his, "mv", ".ai-factory/ARCHITECTURE.md", ".ai-factory/plans/moved-architecture.md")
    _push_his_branch(stand, move)

    _seed_bases(stand, stand.base)
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

    _seed_bases(stand, stand.base)
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
    (project / ".ai-factory" / "plans" / "01-slug.json").write_text(json.dumps({"bases": [stand.base]}))
    preserved = tuple(project / ".ai-factory" / name for name in ("plans", "plan-reviews", "reviews", "test-runs"))
    result = HermesResult(project, (Repository(directory=project, folder=None),), preserved)

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

    _seed_bases(stand, stand.base)
    _result(stand).lay(stand.factory / "plans" / "01-slug.md")

    assert (stand.run / "a.txt").read_text() == "first iteration\n"
    assert _ref_held(stand.run, LAID)  # the lay advanced its own ref, whatever the clone's refspec is

    def second(his):
        git_in(his, "checkout", stand.base, "--", "sub/s.txt")
    second(stand.his)
    git_in(stand.his, "add", "-A")
    git_in(stand.his, "commit", "-m", "the fixes")
    git_in(stand.his, "push", "origin", BRANCH)

    _seed_bases(stand, stand.base)
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

    _seed_bases(stand, stand.base)
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

    _seed_bases(stand, stand.base)
    implementer.implement(plan, answer)

    assert observed["branch_at_call_time"] == ""
    (call,) = backend.calls
    assert call.prompt.endswith(
        f"In the project's repository, create the branch {BRANCH} from the base "
        "commit, commit your work to it, and push it to origin."
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

    _seed_bases(stand, stand.base)
    implementer.implement(plan, stand.run / ".ai-factory" / "reviews" / "01-slug-implementer-2.md")

    (call,) = backend.calls
    assert call.prompt.endswith(
        f"In the project's repository, continue the branch {BRANCH}, "
        "commit your work to it, and push it to origin."
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

    def run(self, *, role, prompt, cwd, instructions, session_id=None, bases=None):
        self.calls.append(SimpleNamespace(role=role, prompt=prompt, cwd=cwd, instructions=instructions, session_id=session_id, bases=bases))
        result = self.on_call(self.calls[-1]) if self.on_call is not None else None
        return (result if isinstance(result, str) else "fake output"), "his-session"


def test_the_branch_name_is_the_task_s_own_name():
    assert branch_of("07-other-task") == "orchestrator/07-other-task"
    assert branch_of("john-doe/07-other-task") == "orchestrator/john-doe/07-other-task"


def _linked_stand(tmp_path):
    """A project whose `.ai-factory` is a tracked link into another repository
    holding two projects' folders: the run's tree is both, and his clone of
    each origin builds his branch of each, the other repository's carrying
    the project's folder."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "README.md").write_text("shared\n")
    git_in(shared, "init", "-q", "-b", "main", ".")
    (shared / "ai" / "p1" / "plans").mkdir(parents=True)
    (shared / "ai" / "p1" / "plans" / "01-slug.md").write_text("# Plan\n")
    (shared / "ai" / "p1" / "notes.md").write_text("a folder file his work changes\n")
    (shared / "ai" / "p2").mkdir(parents=True)  # another project's folder of the same repository
    git_in(shared, "add", "-A")
    git_in(shared, "commit", "-q", "-m", "the shared base")
    shared_base = git_in(shared, "rev-parse", "HEAD").strip()
    shared_origin = tmp_path / "shared-origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(shared_origin))
    git_in(shared, "remote", "add", "origin", str(shared_origin))
    git_in(shared, "push", "-q", "origin", "HEAD:refs/heads/main")

    project = tmp_path / "project"
    project.mkdir()
    (project / "code.txt").write_text("base\n")
    project.joinpath(".ai-factory").symlink_to(shared / "ai" / "p1")
    git_in(project, "init", "-q", "-b", "main", ".")
    git_in(project, "add", "-A")
    git_in(project, "commit", "-q", "-m", "the base")
    project_base = git_in(project, "rev-parse", "HEAD").strip()
    project_origin = tmp_path / "project-origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(project_origin))
    git_in(project, "remote", "add", "origin", str(project_origin))
    git_in(project, "push", "-q", "origin", "HEAD:refs/heads/main")

    his_project = tmp_path / "his-project"
    git_in(tmp_path, "clone", "-q", str(project_origin), str(his_project))
    his_shared = tmp_path / "his-shared"
    git_in(tmp_path, "clone", "-q", str(shared_origin), str(his_shared))
    factory = project / ".ai-factory"
    preserved = tuple(
        factory / name for name in ("plans", "plan-reviews", "reviews", "test-runs", "dispositions")
    )
    return SimpleNamespace(
        project=project, shared=shared, factory=factory,
        project_origin=project_origin, shared_origin=shared_origin,
        project_base=project_base, shared_base=shared_base,
        his_project=his_project, his_shared=his_shared,
        tree=find_run_tree(project), preserved=preserved,
        plan=factory / "plans" / "01-slug.md",
    )


def test_the_channel_names_each_repository_of_the_tree_and_its_own_branch(tmp_path):
    """One sentence per repository of the tree from one template: each names
    its repository by its label and the branch as that repository names it,
    the other repository's carrying the project's folder, created from the
    base commit on the first call and continued afterwards."""
    stand = _linked_stand(tmp_path)
    assert len(stand.tree) == 2
    result = HermesResult(stand.project, stand.tree, stand.preserved)

    assert result.channel_text(stand.plan, first=True) == (
        "Channel: implement on your own branch, in every repository of the "
        "run's tree, one you did not change included, where it stays at its "
        "base. In the project's repository, create the branch "
        "orchestrator/01-slug from the base commit, commit your work to it, "
        "and push it to origin. In the repository holding `.ai-factory`, its "
        "folder `ai/p1/`, create the branch orchestrator/ai/p1/01-slug from "
        "the base commit, commit your work to it, and push it to origin."
    )
    assert result.channel_text(stand.plan, first=False) == (
        "Channel: implement on your own branch, in every repository of the "
        "run's tree, one you did not change included, where it stays at its "
        "base. In the project's repository, continue the branch "
        "orchestrator/01-slug, commit your work to it, and push it to origin. "
        "In the repository holding `.ai-factory`, its folder `ai/p1/`, "
        "continue the branch orchestrator/ai/p1/01-slug, commit your work to "
        "it, and push it to origin."
    )


def test_prepare_removes_each_repository_s_leftover_by_its_own_name(tmp_path):
    """The removal runs in each repository of the tree with that repository's
    names: the branch and laid ref in the other repository carry the project's
    folder, so another project's branch of the same task name in the shared
    repository stays, as do the iteration's refs."""
    stand = _linked_stand(tmp_path)
    git_in(stand.his_project, "push", "-q", "origin", f"{stand.project_base}:refs/heads/orchestrator/01-slug")
    git_in(stand.his_shared, "push", "-q", "origin", f"{stand.shared_base}:refs/heads/orchestrator/ai/p1/01-slug")
    git_in(stand.his_shared, "push", "-q", "origin", f"{stand.shared_base}:refs/heads/orchestrator/ai/p2/01-slug")  # another project's
    git_in(stand.his_project, "push", "-q", "origin", f"{stand.project_base}:refs/orchestrator-iteration/01-slug")
    git_in(stand.his_shared, "push", "-q", "origin", f"{stand.shared_base}:refs/orchestrator-iteration/ai/p1/01-slug")
    git_in(stand.project, "update-ref", "refs/orchestrator-laid/01-slug", stand.project_base)
    git_in(stand.shared, "update-ref", "refs/orchestrator-laid/ai/p1/01-slug", stand.shared_base)

    HermesResult(stand.project, stand.tree, stand.preserved).prepare(stand.plan)

    assert _heads(stand.project_origin, "orchestrator/01-slug") == ""
    assert _heads(stand.shared_origin, "orchestrator/ai/p1/01-slug") == ""
    assert _heads(stand.shared_origin, "orchestrator/ai/p2/01-slug")  # another project's branch of the same task name
    assert _remote_ref(stand.project_origin, "refs/orchestrator-iteration/01-slug")
    assert _remote_ref(stand.shared_origin, "refs/orchestrator-iteration/ai/p1/01-slug")
    assert not _ref_held(stand.project, "refs/orchestrator-laid/01-slug")
    assert not _ref_held(stand.shared, "refs/orchestrator-laid/ai/p1/01-slug")


def test_a_lay_in_each_repository_lays_his_changes_and_keeps_the_other_s_artifact_folders(tmp_path):
    """His branch of each repository lays in its repository, relative to the
    folder in the other: his change outside the artifact paths lands, his
    change inside them does not — the run's own artifact folders of that
    repository are kept — and the project's lay, whose preserved paths all
    lie in the other repository, lays his code change and raises nothing."""
    stand = _linked_stand(tmp_path)
    git_in(stand.his_project, "checkout", "-q", "-B", "orchestrator/01-slug", stand.project_base)
    (stand.his_project / "code.txt").write_text("his version\n")
    git_in(stand.his_project, "commit", "-q", "-am", "his project work")
    git_in(stand.his_project, "push", "-q", "origin", "HEAD:refs/heads/orchestrator/01-slug")
    git_in(stand.his_shared, "checkout", "-q", "-B", "orchestrator/ai/p1/01-slug", stand.shared_base)
    (stand.his_shared / "ai" / "p1" / "notes.md").write_text("his folder work\n")
    (stand.his_shared / "ai" / "p1" / "plans" / "01-slug.md").write_text("# His plan\n")
    git_in(stand.his_shared, "commit", "-q", "-am", "his shared work")
    git_in(stand.his_shared, "push", "-q", "origin", "HEAD:refs/heads/orchestrator/ai/p1/01-slug")
    his_project_tip = git_in(stand.his_project, "rev-parse", "HEAD").strip()
    his_shared_tip = git_in(stand.his_shared, "rev-parse", "HEAD").strip()
    (stand.shared / "ai" / "p1" / "plans" / "01-slug.md").write_text("# The run's plan\n")  # the run's own artifact, uncommitted

    _seed_bases(stand, stand.project_base, stand.shared_base)
    HermesResult(stand.project, stand.tree, stand.preserved).lay(stand.plan)

    assert (stand.project / "code.txt").read_text() == "his version\n"
    assert git_in(stand.project, "rev-parse", "HEAD").strip() == stand.project_base  # HEAD moves nowhere
    assert (stand.shared / "ai" / "p1" / "notes.md").read_text() == "his folder work\n"
    assert (stand.factory / "plans" / "01-slug.md").read_text() == "# The run's plan\n"  # his copy never laid over it
    assert git_in(stand.shared, "rev-parse", "HEAD").strip() == stand.shared_base
    assert git_in(stand.project, "rev-parse", "refs/orchestrator-laid/01-slug").strip() == his_project_tip
    assert git_in(stand.shared, "rev-parse", "refs/orchestrator-laid/ai/p1/01-slug").strip() == his_shared_tip


def test_a_repository_he_did_not_change_lays_nothing_and_advances_no_laid_ref(tmp_path):
    """A branch held at its base — a repository he did not change — lays
    nothing and advances no laid ref, an uncommitted edit left alone, and the
    project's repository, whose preserved paths all lie in the other, raises
    nothing the while."""
    stand = _linked_stand(tmp_path)
    git_in(stand.his_project, "push", "-q", "origin", f"{stand.project_base}:refs/heads/orchestrator/01-slug")
    git_in(stand.his_shared, "push", "-q", "origin", f"{stand.shared_base}:refs/heads/orchestrator/ai/p1/01-slug")
    (stand.shared / "ai" / "p1" / "notes.md").write_text("the run's uncommitted edit\n")

    _seed_bases(stand, stand.project_base, stand.shared_base)
    HermesResult(stand.project, stand.tree, stand.preserved).lay(stand.plan)

    assert (stand.shared / "ai" / "p1" / "notes.md").read_text() == "the run's uncommitted edit\n"
    assert not _ref_held(stand.shared, "refs/orchestrator-laid/ai/p1/01-slug")  # nothing laid there, no ref advanced
    assert not _ref_held(stand.project, "refs/orchestrator-laid/01-slug")  # nothing laid there either
    assert git_in(stand.project, "rev-parse", "HEAD").strip() == stand.project_base
    assert git_in(stand.shared, "rev-parse", "HEAD").strip() == stand.shared_base


def test_a_commit_landing_in_the_shared_repository_after_the_record_moves_no_base(tmp_path):
    """A repository several projects share takes another project's head's
    commit while the run works in it: the task's base stays the commit the
    record holds, so the lay raises nothing over his branch — built from
    that base — lays exactly the paths his branch changed, and neither the
    other project's committed change nor the run's own artifacts lay."""
    stand = _linked_stand(tmp_path)
    # The task's start: the record written with each repository's base.
    _seed_bases(stand, stand.project_base, stand.shared_base)
    # Another project's head commits in the shared repository after the
    # record is written and before the lay, moving its HEAD past the base.
    (stand.shared / "ai" / "p2" / "late.md").write_text("another project's late commit\n")
    git_in(stand.shared, "add", "ai/p2/late.md")
    git_in(stand.shared, "commit", "-q", "-m", "another project's late commit")
    moved = git_in(stand.shared, "rev-parse", "HEAD").strip()
    assert moved != stand.shared_base
    # His branches: the shared one built from the recorded base, changing a
    # file of the project's folder and one of the run's artifact paths; the
    # project's own held at its base.
    git_in(stand.his_shared, "checkout", "-q", "-B", "orchestrator/ai/p1/01-slug", stand.shared_base)
    (stand.his_shared / "ai" / "p1" / "notes.md").write_text("his folder work\n")
    (stand.his_shared / "ai" / "p1" / "plans" / "01-slug.md").write_text("# His plan\n")
    git_in(stand.his_shared, "commit", "-q", "-am", "his shared work")
    git_in(stand.his_shared, "push", "-q", "origin", "HEAD:refs/heads/orchestrator/ai/p1/01-slug")
    git_in(stand.his_project, "push", "-q", "origin", f"{stand.project_base}:refs/heads/orchestrator/01-slug")
    his_shared_tip = git_in(stand.his_shared, "rev-parse", "HEAD").strip()
    (stand.shared / "ai" / "p1" / "plans" / "01-slug.md").write_text("# The run's plan\n")  # the run's own, uncommitted

    HermesResult(stand.project, stand.tree, stand.preserved).lay(stand.plan)  # raises nothing

    assert (stand.shared / "ai" / "p1" / "notes.md").read_text() == "his folder work\n"  # his change lays
    assert (stand.factory / "plans" / "01-slug.md").read_text() == "# The run's plan\n"  # the artifact path never lays
    assert (stand.shared / "ai" / "p2" / "late.md").read_text() == "another project's late commit\n"  # untouched by the lay
    assert git_in(stand.shared, "rev-parse", "HEAD").strip() == moved  # HEAD stays where the other actor left it
    assert git_in(stand.project, "rev-parse", "HEAD").strip() == stand.project_base
    assert git_in(stand.shared, "rev-parse", "refs/orchestrator-laid/ai/p1/01-slug").strip() == his_shared_tip
    assert not _ref_held(stand.project, "refs/orchestrator-laid/01-slug")  # a repository he did not change lays nothing


@pytest.mark.parametrize("shape", ["files", "a regular file", "an embedded repository"])
def test_a_project_branch_holding_the_placed_factory_lays_nothing_under_it_and_keeps_the_link(
    tmp_path, shape,
):
    """His clone places the other repository's folder where his clone of the
    project expects `.ai-factory`, and his project branch may then carry the
    placed folder as files, as a type change from the tracked link or as a
    gitlink for an embedded clone: the project repository's lay keeps the
    run's link exactly as it is — no path at or under `.ai-factory` is ever
    removed or restored — while his change outside it lands and the laid ref
    advances as for any other lay."""
    stand = _linked_stand(tmp_path)
    (stand.factory / "plans" / "01-slug.md").write_text("# The run's plan\n")  # the run's own, uncommitted
    his = stand.his_project
    git_in(his, "checkout", "-q", "-B", "orchestrator/01-slug", stand.project_base)
    (his / "code.txt").write_text("his version\n")  # outside `.ai-factory`, so the lay has his work
    factory = his / ".ai-factory"
    factory.unlink()  # the placed folder replaces the tracked link his clone holds
    if shape == "files":
        factory.mkdir()
        (factory / "notes.md").write_text("his memory\n")
    elif shape == "a regular file":
        factory.write_text("his file where the link was\n")
    else:
        git_in(his, "init", "-q", "-b", "main", str(factory))
        git_in(factory, "commit", "-q", "--allow-empty", "-m", "his embedded clone")
    git_in(his, "add", "-A")
    git_in(his, "commit", "-q", "-m", "his project work")
    git_in(his, "push", "-q", "origin", "HEAD:refs/heads/orchestrator/01-slug")
    git_in(stand.his_shared, "push", "-q", "origin", f"{stand.shared_base}:refs/heads/orchestrator/ai/p1/01-slug")
    his_project_tip = git_in(his, "rev-parse", "HEAD").strip()

    _seed_bases(stand, stand.project_base, stand.shared_base)
    HermesResult(stand.project, stand.tree, stand.preserved).lay(stand.plan)

    assert (stand.project / "code.txt").read_text() == "his version\n"  # his work outside it lays
    assert stand.factory.is_symlink()  # the run's link stays a link
    assert stand.factory.readlink() == stand.shared / "ai" / "p1"  # to where it always led
    assert (stand.factory / "plans" / "01-slug.md").read_text() == "# The run's plan\n"  # read through the run's link
    assert git_in(stand.project, "rev-parse", "HEAD").strip() == stand.project_base  # HEAD moves nowhere
    assert git_in(stand.project, "rev-parse", "refs/orchestrator-laid/01-slug").strip() == his_project_tip
    assert git_in(stand.shared, "rev-parse", "HEAD").strip() == stand.shared_base  # the other repository's lay is as it is

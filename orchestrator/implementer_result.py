"""The implementer's result: how a turn's work reaches the run's working tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import git_runner
from .errors import (
    BranchNotFetchedError,
    BranchNotFromBaseError,
    LeftoverBranchNotRemovedError,
)
from .git_runner import run_git_sync
from .ports import ImplementerResultProtocol


class WorkingTreeResult:
    """The result of an implementer that works in the run's tree: its work already
    stands there, so the preparation and the lay do nothing and the channel is empty."""

    def prepare(self, plan_path: Path) -> None:
        """No-op: an implementer in the run's tree needs no preparation."""

    def channel_text(self, plan_path: Path, *, first: bool) -> str:
        """Empty: an implementer in the run's tree needs no channel."""
        return ""

    def lay(self, plan_path: Path) -> None:
        """No-op: an implementer in the run's tree lays its work as it goes."""


def branch_of(plan_path: Path) -> str:
    """The branch a task's implementer pushes: the plan's own stem under orchestrator/."""
    return f"orchestrator/{plan_path.stem}"


def _remove_leftover_branch(cwd: Path, branch: str) -> None:
    """Remove `branch` from origin when origin holds it; every fault raises."""
    held = run_git_sync(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=cwd)
    if held.returncode != 0:
        raise LeftoverBranchNotRemovedError(
            "A leftover branch could not be looked up on the remote\n"
            f"{branch}\n{held.stderr.strip()[:500]}"
        )
    if not held.stdout.strip():
        return
    pushed = run_git_sync(["push", "origin", "--delete", branch], cwd=cwd)
    if pushed.returncode != 0:
        raise LeftoverBranchNotRemovedError(
            "A branch left on the remote could not be removed\n"
            f"{branch}\n{pushed.stderr.strip()[:500]}"
        )


def _drop_tracking_ref(cwd: Path, branch: str) -> None:
    """Remove the branch's remote-tracking ref from the clone, so no earlier
    attempt's tip is read as this task's previous lay; a ref that does not
    exist is not a failure."""
    ref = f"refs/remotes/origin/{branch}"
    dropped = run_git_sync(["update-ref", "-d", ref], cwd=cwd)
    if dropped.returncode != 0:
        raise LeftoverBranchNotRemovedError(
            "A leftover branch's remote-tracking ref could not be removed\n"
            f"{branch}\n{dropped.stderr.strip()[:500]}"
        )


def _previous_tip(cwd: Path, branch: str, base: str) -> str:
    """The tip the previous lay of this task's branch laid: the remote-tracking
    ref as it stands before this lay's own fetch updates it; a missing ref is
    the base."""
    ref = f"refs/remotes/origin/{branch}"
    rev = run_git_sync(["rev-parse", "--verify", "--quiet", ref], cwd=cwd)
    resolved = rev.stdout.strip() if rev.returncode == 0 else ""
    return resolved or base


def _changed_paths(cwd: Path, base: str, tip: str) -> set[str]:
    """The paths that differ between the two commits, relative to `cwd`, a
    rename listed as the deletion of its source and the addition of its
    destination whatever the repository's `diff.renames` setting says."""
    diff = run_git_sync(
        ["diff", "--no-renames", "--name-only", "-z", "--relative", base, tip], cwd=cwd,
    )
    return {path for path in diff.stdout.split("\0") if path}


def _lay_branch_tree(cwd: Path, branch: str, preserved: tuple[Path, ...]) -> None:
    """Lay exactly the paths his work changed, HEAD staying put.

    The paths to lay are those that differ between the base and his new tip,
    together with those that differ between the base and the tip the previous
    lay of this task's branch laid, minus the run's own artifact paths, all in
    one coordinate system relative to `cwd`. Removals precede writes, so a
    path that turns from a file into a directory, or the reverse, lays without
    error: a path the tip does not hold is removed first, then a path the tip
    holds is written as the tip holds it, mode included. Every other path
    stays exactly as the run left it, an uncommitted edit included. Nothing is
    merged, HEAD does not move, and a tip of which the base is not an ancestor
    lays nothing.
    """
    base = run_git_sync(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    previous = _previous_tip(cwd, branch, base)
    try:
        fetched = run_git_sync(
            ["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        raise BranchNotFetchedError(
            "The implementer's branch could not be fetched\n"
            f"{branch}\ntimed out after {git_runner.SYNC_GIT_TIMEOUT} seconds"
        ) from None
    if fetched.returncode != 0:
        raise BranchNotFetchedError(
            "The implementer's branch could not be fetched\n"
            f"{branch}\n{fetched.stderr.strip()[:500]}"
        )
    if run_git_sync(["merge-base", "--is-ancestor", base, "FETCH_HEAD"], cwd=cwd).returncode != 0:
        raise BranchNotFromBaseError(
            "The implementer's branch does not descend from the base\n"
            f"{branch}\n{base}"
        )
    roots = tuple(
        path.resolve().relative_to(cwd.resolve()).as_posix() for path in preserved
    )
    changed = _changed_paths(cwd, base, "FETCH_HEAD") | _changed_paths(cwd, base, previous)
    changed = {
        path for path in changed
        if not any(path == root or path.startswith(f"{root}/") for root in roots)
    }
    if not changed:
        return
    held = run_git_sync(["ls-tree", "-r", "--name-only", "-z", "FETCH_HEAD"], cwd=cwd)
    in_tip = {path for path in held.stdout.split("\0") if path}
    removed = sorted(changed - in_tip)
    written = sorted(changed & in_tip)
    if removed:
        gone = run_git_sync(["rm", "-f", "--ignore-unmatch", "--", *removed], cwd=cwd)
        if gone.returncode != 0:
            raise RuntimeError(
                "Hermes implementer branch could not be laid as the working tree\n"
                f"{branch}\n{gone.stderr.strip()[:500]}"
            )
    if written:
        restored = run_git_sync(
            ["restore", "--source=FETCH_HEAD", "--staged", "--worktree", "--", *written], cwd=cwd,
        )
        if restored.returncode != 0:
            raise RuntimeError(
                "Hermes implementer branch could not be laid as the working tree\n"
                f"{branch}\n{restored.stderr.strip()[:500]}"
            )


class HermesResult:
    """The result of an implementer on Hermes: a branch he pushes, laid as the tree.

    His work happens in his own clone on `orchestrator/<seq>-<slug>`, the same branch
    across the task's fix iterations. The task's first call never meets the branch of
    an earlier attempt, nor the remote-tracking ref an earlier attempt's fetch left,
    and his answer's lay fetches the branch's tip and changes exactly the paths his
    work changed, outside the run's own artifact paths.
    """

    def __init__(self, project_dir: Path, preserved: tuple[Path, ...]) -> None:
        self.project_dir = project_dir
        self.preserved = tuple(preserved)

    def prepare(self, plan_path: Path) -> None:
        """Remove a leftover branch of an earlier attempt from origin and its
        remote-tracking ref from the clone, so the task's first call never
        builds on it silently."""
        branch = branch_of(plan_path)
        _remove_leftover_branch(self.project_dir, branch)
        _drop_tracking_ref(self.project_dir, branch)

    def channel_text(self, plan_path: Path, *, first: bool) -> str:
        """Name the branch to push, and how to reach it: created from the base commit
        on the task's first call, continued on a call that continues his session."""
        branch = branch_of(plan_path)
        if first:
            return (
                f"Channel: implement on your own branch. Create the branch {branch} "
                "from the base commit, commit your work to it, and push it to origin."
            )
        return (
            f"Channel: implement on your own branch. Continue the branch {branch}, "
            "commit your work to it, and push it to origin."
        )

    def lay(self, plan_path: Path) -> None:
        """Lay the paths his branch changed as the run's working tree, outside the run's own artifacts."""
        _lay_branch_tree(self.project_dir, branch_of(plan_path), self.preserved)

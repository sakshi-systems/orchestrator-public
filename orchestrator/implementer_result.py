"""The implementer's result: how a turn's work reaches the run's working tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import git_runner
from .artifact_layout import PLANS_DIRNAME, artifact_dir
from .errors import (
    BranchNotFetchedError,
    BranchNotFromBaseError,
    LeftoverBranchNotRemovedError,
)
from .git_runner import _changed_paths, run_git_sync
from .ports import ImplementerResultProtocol, Repository


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


def ref_name_of(project_dir: Path, plan_path: Path) -> str:
    """The task's own name: the plan's stem under its roadmap's artifact segment.

    A plan in the default pair names itself by its stem alone, `01-slug`; a
    plan a named roadmap holds, under `plans/<segment>/`, carries that segment
    before its stem, `john-doe/01-slug`, so two roadmaps whose tasks share a
    stem never share a name.
    """
    segment = plan_path.parent.relative_to(artifact_dir(project_dir, PLANS_DIRNAME, None))
    return "/".join((*segment.parts, plan_path.stem))


def branch_of(name: str) -> str:
    """The branch a task's implementer pushes: the task's own name under orchestrator/."""
    return f"orchestrator/{name}"


def laid_ref_of(name: str) -> str:
    """The run's own ref that holds the tip the last lay applied, for a task
    named as its repository names it.

    The ref lives in the run's clone alone, under a space the iteration ref
    and the branch share with nothing, is never pushed and is written by no
    fetch — only a lay that has succeeded advances it.
    """
    return f"refs/orchestrator-laid/{name}"


def _remove_leftover_branch(cwd: Path, branch: str) -> None:
    """Remove `branch` from origin when origin holds it; every fault raises.

    The deletion names the branch by its full ref, `refs/heads/<branch>`, so an
    iteration ref origin holds under a similar name never makes the short name
    ambiguous.
    """
    held = run_git_sync(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=cwd)
    if held.returncode != 0:
        raise LeftoverBranchNotRemovedError(
            "A leftover branch could not be looked up on the remote\n"
            f"{branch}\n{held.stderr.strip()[:500]}"
        )
    if not held.stdout.strip():
        return
    pushed = run_git_sync(["push", "origin", "--delete", f"refs/heads/{branch}"], cwd=cwd)
    if pushed.returncode != 0:
        raise LeftoverBranchNotRemovedError(
            "A branch left on the remote could not be removed\n"
            f"{branch}\n{pushed.stderr.strip()[:500]}"
        )


def _drop_laid_ref(cwd: Path, laid_ref: str) -> None:
    """Remove the run's own laid ref — the tip the last lay of an earlier
    attempt applied — so it is not read as this task's previous lay; a ref
    that does not exist is not a failure."""
    dropped = run_git_sync(["update-ref", "-d", laid_ref], cwd=cwd)
    if dropped.returncode != 0:
        raise LeftoverBranchNotRemovedError(
            "A leftover branch's laid tip could not be removed\n"
            f"{laid_ref}\n{dropped.stderr.strip()[:500]}"
        )


def _previous_tip(cwd: Path, laid_ref: str, base: str) -> str:
    """The tip the last lay of this task's branch applied: the run's own laid
    ref as it stands, written by no fetch; a missing ref is the base."""
    rev = run_git_sync(["rev-parse", "--verify", "--quiet", laid_ref], cwd=cwd)
    resolved = rev.stdout.strip() if rev.returncode == 0 else ""
    return resolved or base


def _lay_branch_tree(
    cwd: Path,
    branch: str,
    laid_ref: str,
    preserved: tuple[Path, ...],
    *,
    as_spelled: tuple[Path, ...] = (),
) -> None:
    """Lay exactly the paths his work changed, HEAD staying put.

    The paths to lay are those that differ between the base and his new tip,
    together with those that differ between the base and the tip the last lay
    of this task's branch applied — the run's own laid ref — minus the run's
    own artifact paths the repository holds, all in one coordinate system
    relative to `cwd`; a preserved path that lies in another repository of
    the run's tree is left out, its lay the only one that keeps it. A path
    of `as_spelled` is preserved as the project spells it, relative to `cwd`
    and never through `resolve()` — wherever a link the project holds at it
    leads — so nothing his branch holds at or under the path itself is laid
    through or over the run's own.
    Removals precede writes, so a path that turns from a file into a
    directory, or the reverse, lays without error: a path the tip does not
    hold is removed first, then a path the tip holds is written as the tip
    holds it, mode included. Every other path stays exactly as the run left
    it, an uncommitted edit included. Nothing is merged, HEAD does not move,
    and a tip of which the base is not an ancestor lays nothing.

    The fetch moves `FETCH_HEAD` alone — no remote-tracking ref is written —
    and the laid ref is advanced to the fetched tip only once the removals
    and the restores have succeeded, so a lay that fails leaves it where it
    was and its retry restores every path again, whatever the run fetched
    from origin in between.
    """
    base = run_git_sync(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    previous = _previous_tip(cwd, laid_ref, base)
    try:
        fetched = run_git_sync(["fetch", "--refmap=", "origin", f"refs/heads/{branch}"], cwd=cwd)
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
    tip = run_git_sync(["rev-parse", "FETCH_HEAD"], cwd=cwd).stdout.strip()
    resolved_cwd = cwd.resolve()
    roots = []
    for path in preserved:
        try:
            roots.append(path.resolve().relative_to(resolved_cwd).as_posix())
        except ValueError:
            # A preserved path of another repository of the run's tree: this
            # repository's lay never touches it.
            continue
    for path in as_spelled:
        try:
            roots.append(path.relative_to(cwd).as_posix())
        except ValueError:
            # A path spelled outside this repository: no path of this lay
            # lies under it.
            continue
    roots = tuple(roots)
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
    advanced = run_git_sync(["update-ref", laid_ref, tip], cwd=cwd)
    if advanced.returncode != 0:
        raise RuntimeError(
            "Hermes implementer branch could not be laid as the working tree\n"
            f"{branch}\n{advanced.stderr.strip()[:500]}"
        )


class HermesResult:
    """The result of an implementer on Hermes: a branch he pushes to every
    origin of the run's tree, laid as each repository's tree.

    His work happens in his own clone on the branch his task's plan names
    under `orchestrator/` — its roadmap's segment and the plan's stem, the
    other repository's folder before it — the same branch across the task's
    fix iterations, pushed to every origin of the tree, a repository he did
    not change holding it at its base. The task's first call never meets the
    branch of an earlier attempt, nor the laid ref an earlier attempt's lay
    left, in any repository, and his answer's lay fetches each branch's tip
    in its repository and changes exactly the paths his work changed there,
    outside the run's own artifact paths that lie in it. Where the tree
    holds two repositories, the path `.ai-factory` is preserved in the
    project repository's lay beside them, as the project spells it and not
    where its link leads, so nothing his project branch holds under it —
    files, a type change from the link or an embedded repository — is laid
    through or over the run's own.
    """

    def __init__(
        self,
        project_dir: Path,
        tree: tuple[Repository, ...],
        preserved: tuple[Path, ...],
    ) -> None:
        self.project_dir = project_dir
        self.tree = tuple(tree)
        self.preserved = tuple(preserved)

    def prepare(self, plan_path: Path) -> None:
        """Remove a leftover branch of an earlier attempt from each
        repository's origin and its laid tip — the tip the attempt's last lay
        applied, kept in a ref of the run's own — from each repository's
        clone, every ref named as its repository names them, so the task's
        first call never builds on them silently."""
        name = ref_name_of(self.project_dir, plan_path)
        for repository in self.tree:
            repository_name = repository.named(name)
            _remove_leftover_branch(repository.directory, branch_of(repository_name))
            _drop_laid_ref(repository.directory, laid_ref_of(repository_name))

    def channel_text(self, plan_path: Path, *, first: bool) -> str:
        """Name, for each repository of the run's tree, the branch to push —
        named as that repository names it — and how to reach it: created from
        that repository's base commit on the task's first call, continued on
        a call that continues his session."""
        name = ref_name_of(self.project_dir, plan_path)
        sentences = []
        for repository in self.tree:
            branch = branch_of(repository.named(name))
            if first:
                body = (
                    f"create the branch {branch} from the base commit, "
                    "commit your work to it, and push it to origin."
                )
            else:
                body = (
                    f"continue the branch {branch}, "
                    "commit your work to it, and push it to origin."
                )
            sentences.append(f"In {repository.label}, {body}")
        return (
            "Channel: implement on your own branch, in every repository of the "
            "run's tree, one you did not change included, where it stays at its "
            f"base. {' '.join(sentences)}"
        )

    def lay(self, plan_path: Path) -> None:
        """Lay, in each repository of the run's tree in order, the paths his
        branch there changed as its working tree, outside the run's own
        artifacts that lie in it — and, where the tree holds two
        repositories, outside `.ai-factory` in the project's repository
        alone, kept as the project spells it wherever its link leads; a
        branch an origin does not hold halts as the fetch fault."""
        name = ref_name_of(self.project_dir, plan_path)
        for repository in self.tree:
            repository_name = repository.named(name)
            as_spelled: tuple[Path, ...] = ()
            if len(self.tree) > 1 and repository.folder is None:
                as_spelled = (self.project_dir / ".ai-factory",)
            _lay_branch_tree(
                repository.directory,
                branch_of(repository_name),
                laid_ref_of(repository_name),
                self.preserved,
                as_spelled=as_spelled,
            )

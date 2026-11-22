"""The iteration's state as a commit on the run's ref."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import git_runner
from .errors import IterationCommitNotMadeError, IterationRefNotPushedError
from .git_runner import run_git_sync
from .implementer_result import branch_of, laid_ref_of, ref_name_of
from .ports import IterationState


def ref_of(name: str) -> str:
    """The ref the task's iterations are committed on: refs/orchestrator-iteration/<name>."""
    return f"refs/orchestrator-iteration/{name}"


def make_iteration_commit(project_dir: Path, name: str, iteration: int) -> IterationState:
    """Write the real index's tree as a commit on the task's ref.

    The tree is the real index's, as the run's `git add -A` left it, so the
    commit holds what the task's own commit would, `.ai-factory` included; the
    parent is the base the task began on, so each iteration's commit holds the
    task's whole state since the base; and the ref `refs/orchestrator-iteration/<name>`
    is set to it, one ref for the task, moved to each iteration's commit in
    turn. Nothing is staged, HEAD does not move, and no call names a pathspec,
    so literal pathspecs change none of them. A call that exits non-zero halts
    as IterationCommitNotMadeError; a timeout surfaces as the
    subprocess.TimeoutExpired it is.
    """
    ref = ref_of(name)
    base = run_git_sync(["rev-parse", "HEAD"], cwd=project_dir)
    if base.returncode != 0:
        raise IterationCommitNotMadeError(
            f"The iteration's commit could not be made\n{ref}\n{base.stderr.strip()[:500]}"
        )
    tree = run_git_sync(["write-tree"], cwd=project_dir)
    if tree.returncode != 0:
        raise IterationCommitNotMadeError(
            f"The iteration's commit could not be made\n{ref}\n{tree.stderr.strip()[:500]}"
        )
    commit = run_git_sync(
        ["commit-tree", tree.stdout.strip(), "-p", base.stdout.strip(), "-m", f"{name}: iteration {iteration}"],
        cwd=project_dir,
    )
    if commit.returncode != 0:
        raise IterationCommitNotMadeError(
            f"The iteration's commit could not be made\n{ref}\n{commit.stderr.strip()[:500]}"
        )
    updated = run_git_sync(["update-ref", ref, commit.stdout.strip()], cwd=project_dir)
    if updated.returncode != 0:
        raise IterationCommitNotMadeError(
            f"The iteration's commit could not be made\n{ref}\n{updated.stderr.strip()[:500]}"
        )
    return IterationState(base=base.stdout.strip(), commit=commit.stdout.strip(), ref=ref)


def push_iteration_ref(project_dir: Path, state: IterationState) -> None:
    """Push the iteration's ref to origin, so a reviewer off the machine reads
    the same ref a local one does.

    The push is forced, because each iteration's commit is a sibling of the
    last. A push that fails, a missing origin included, halts as
    IterationRefNotPushedError; a push that times out halts as the same error
    with the bound's text in place of git's.
    """
    try:
        pushed = run_git_sync(["push", "--force", "origin", f"{state.ref}:{state.ref}"], cwd=project_dir)
    except subprocess.TimeoutExpired:
        raise IterationRefNotPushedError(
            "The iteration's ref could not be pushed\n"
            f"{state.ref}\ntimed out after {git_runner.SYNC_GIT_TIMEOUT} seconds"
        ) from None
    if pushed.returncode != 0:
        raise IterationRefNotPushedError(
            f"The iteration's ref could not be pushed\n{state.ref}\n{pushed.stderr.strip()[:500]}"
        )


def remove_iteration_refs(project_dir: Path, plan_path: Path) -> None:
    """Remove the task's iteration ref, locally and on origin, the branch a
    Hermes implementer pushed, and the run's own laid ref, so the task's
    branch carries one commit.

    The two local refs go in any case; the laid ref is the run's own and never
    travels to origin. The remote steps ask origin once for the ref and the
    branch and delete the refs the listing names exactly in one push, and they
    run whether or not the local removals failed. The listing is read by the
    delimiters git writes, a newline between records and a tab before the ref
    name, so a ref name that holds a character Python treats as a line break
    or a space is read whole. A removal that fails — a non-zero exit of any
    call, a timeout of any of them, or a listing record with no tab — prints
    one warning line and returns; no git fault of the removal raises.
    """
    ref = ref_of(ref_name_of(project_dir, plan_path))
    branch = branch_of(project_dir, plan_path)
    laid = laid_ref_of(project_dir, plan_path)
    failed = False
    for local in (ref, laid):
        try:
            if run_git_sync(["update-ref", "-d", local], cwd=project_dir).returncode != 0:
                failed = True
        except subprocess.TimeoutExpired:
            failed = True
    wanted = {ref, f"refs/heads/{branch}"}
    listed = None
    try:
        listed = run_git_sync(["ls-remote", "origin", ref, f"refs/heads/{branch}"], cwd=project_dir)
    except subprocess.TimeoutExpired:
        failed = True
    if listed is not None:
        if listed.returncode != 0:
            failed = True
        else:
            names = []
            for record in listed.stdout.split("\n"):
                if not record:
                    continue
                _sha, tab, name = record.partition("\t")
                if not tab:
                    failed = True
                    continue
                if name in wanted:
                    names.append(name)
            if names:
                deleted = None
                try:
                    deleted = run_git_sync(["push", "origin", "--delete", *names], cwd=project_dir)
                except subprocess.TimeoutExpired:
                    failed = True
                if deleted is not None and deleted.returncode != 0:
                    failed = True
    if failed:
        print(f">>> WARNING: the iteration's ref {ref} could not be removed, continuing anyway.")

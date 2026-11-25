"""The iteration's state as a commit on the run's ref."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import git_runner
from .errors import IterationCommitNotMadeError, IterationRefNotPushedError
from .git_runner import _as_bytes, run_git_sync
from .implementer_result import branch_of, laid_ref_of, ref_name_of
from .ports import IterationState, Repository


def ref_of(name: str) -> str:
    """The ref the task's iterations are committed on: refs/orchestrator-iteration/<name>."""
    return f"refs/orchestrator-iteration/{name}"


def _halted(result, ref: str) -> None:
    """Halt as the iteration's commit could not be made, naming the ref and
    git's own text cut at the detail limit."""
    if result.returncode != 0:
        raise IterationCommitNotMadeError(
            f"The iteration's commit could not be made\n{ref}\n{result.stderr.strip()[:500]}"
        )


def make_iteration_commit(repository: Repository, task_name: str, iteration: int, base: str) -> IterationState:
    """Write the iteration's state as a commit on the task's ref in one
    repository of the run's tree, `task_name` named as that repository names
    it — the project's own name, the folder before it in another — `base`
    the commit the task began on in that repository, as the task's record
    holds it.

    The tree is the repository's own: the real index's, as the run's staging
    left it, in the project's repository, and — where the repository is one
    several projects share — the base commit's tree with the folder's staged
    state in place of the base's folder, so another project's staged change
    stays out of the ref a reviewer off the machine reads as this task's
    work; the parent is the base the task began on, so each iteration's
    commit holds the task's whole state since the base; and the ref
    `refs/orchestrator-iteration/<name>` is set to it, one ref for the task
    in that repository, moved to each iteration's commit in
    turn. Nothing is staged, HEAD does not move, and no call names a pathspec,
    so literal pathspecs change none of them. A call that exits non-zero halts
    as IterationCommitNotMadeError; a timeout surfaces as the
    subprocess.TimeoutExpired it is.
    """
    ref = ref_of(repository.named(task_name))
    directory = repository.directory

    tree = _iteration_tree(repository, base, ref)
    commit = run_git_sync(
        ["commit-tree", tree, "-p", base, "-m", f"{task_name}: iteration {iteration}"],
        cwd=directory,
    )
    _halted(commit, ref)
    updated = run_git_sync(["update-ref", ref, commit.stdout.strip()], cwd=directory)
    _halted(updated, ref)
    return IterationState(
        repository=repository,
        base=base,
        commit=commit.stdout.strip(),
        ref=ref,
    )


def _iteration_tree(repository: Repository, base: str, ref: str) -> str:
    """The tree an iteration's commit takes in `repository`.

    In the project's repository — and in one whose whole self is the
    `.ai-factory` of a single project, its index shared with no other — the
    tree is the real index's, as the run's staging left it. In a repository
    several projects share, the tree is the base commit's with the folder's
    staged state in place of the base's folder: `write-tree` of that index
    holds every project's staged change, so the build reads the folder's
    entries out of it and lays them over the base's tree in a temporary
    index the calls name — a name the caller chose, not an ambient
    `GIT_INDEX_FILE`, which stays stripped — the base's own folder entries
    removed first, so a deletion the staging laid is taken too. The shared
    index is only read; the temporary index goes when the build ends.
    """
    directory = repository.directory
    if not repository.folder:
        written = run_git_sync(["write-tree"], cwd=directory)
        _halted(written, ref)
        return written.stdout.strip()

    descriptor, name = tempfile.mkstemp(prefix="orchestrator-iteration-index.")
    os.close(descriptor)
    index = Path(name)
    try:
        seeded = run_git_sync(["read-tree", base], cwd=directory, index=index)
        _halted(seeded, ref)
        held = run_git_sync(["ls-tree", "-r", "--name-only", "-z", base, "--", "."], cwd=directory)
        _halted(held, ref)
        if held.stdout:
            dropped = run_git_sync(
                ["update-index", "--force-remove", "-z", "--stdin"],
                cwd=directory, index=index, stdin=_as_bytes(held.stdout),
            )
            _halted(dropped, ref)
        staged = run_git_sync(["ls-files", "-s", "-z", "--full-name", "--", "."], cwd=directory)
        _halted(staged, ref)
        if staged.stdout:
            overlaid = run_git_sync(
                ["update-index", "-z", "--index-info"],
                cwd=directory, index=index, stdin=_as_bytes(staged.stdout),
            )
            _halted(overlaid, ref)
        written = run_git_sync(["write-tree"], cwd=directory, index=index)
        _halted(written, ref)
        return written.stdout.strip()
    finally:
        index.unlink(missing_ok=True)


def push_iteration_ref(state: IterationState) -> None:
    """Push the iteration's ref to the origin of the repository it was made
    in, so a reviewer off the machine reads the same ref a local one does.

    The push is forced, because each iteration's commit is a sibling of the
    last. A push that fails, a missing origin included, halts as
    IterationRefNotPushedError; a push that times out halts as the same error
    with the bound's text in place of git's.
    """
    try:
        pushed = run_git_sync(
            ["push", "--force", "origin", f"{state.ref}:{state.ref}"], cwd=state.repository.directory,
        )
    except subprocess.TimeoutExpired:
        raise IterationRefNotPushedError(
            "The iteration's ref could not be pushed\n"
            f"{state.ref}\ntimed out after {git_runner.SYNC_GIT_TIMEOUT} seconds"
        ) from None
    if pushed.returncode != 0:
        raise IterationRefNotPushedError(
            f"The iteration's ref could not be pushed\n{state.ref}\n{pushed.stderr.strip()[:500]}"
        )


def remove_iteration_refs(project_dir: Path, tree: tuple[Repository, ...], plan_path: Path) -> None:
    """Remove the task's iteration ref, locally and on origin, the branch a
    Hermes implementer pushed, and the run's own laid ref, so the task's
    branch carries one commit — in each repository of the run's tree, every
    name the repository's own, the folder before the task's in another, so
    one project's removal never takes another's refs.

    The two local refs go in any case; the laid ref is the run's own and never
    travels to origin. The remote steps ask origin once for the ref and the
    branch and delete the refs the listing names exactly in one push, and they
    run whether or not the local removals failed. The listing is read by the
    delimiters git writes, a newline between records and a tab before the ref
    name, so a ref name that holds a character Python treats as a line break
    or a space is read whole. A removal that fails — a non-zero exit of any
    call, a timeout of any of them, or a listing record with no tab — prints
    one warning line naming that repository's ref and returns, once for each
    repository it fails in; no git fault of the removal raises.
    """
    task_name = ref_name_of(project_dir, plan_path)
    for repository in tree:
        name = repository.named(task_name)
        _remove_repository_refs(repository.directory, ref_of(name), branch_of(name), laid_ref_of(name))


def _remove_repository_refs(directory: Path, ref: str, branch: str, laid: str) -> None:
    """Remove one repository's refs of the task by today's rule: the local
    ref and the laid ref in any case, and the ref and the branch on origin
    where a listing names them."""
    failed = False
    for local in (ref, laid):
        try:
            if run_git_sync(["update-ref", "-d", local], cwd=directory).returncode != 0:
                failed = True
        except subprocess.TimeoutExpired:
            failed = True
    wanted = {ref, f"refs/heads/{branch}"}
    listed = None
    try:
        listed = run_git_sync(["ls-remote", "origin", ref, f"refs/heads/{branch}"], cwd=directory)
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
                    deleted = run_git_sync(["push", "origin", "--delete", *names], cwd=directory)
                except subprocess.TimeoutExpired:
                    failed = True
                if deleted is not None and deleted.returncode != 0:
                    failed = True
    if failed:
        print(f">>> WARNING: the iteration's ref {ref} could not be removed, continuing anyway.")

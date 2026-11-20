"""Tests for orchestrator.review_workspace: real git against a local
bare origin that holds the pull request's head."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from orchestrator import review_workspace
from orchestrator.git_runner import GitError
from orchestrator.review_workspace import (ReviewWorkspaceError,
                                           ReviewWorkspaces, Workspace)
from orchestrator.reviews import ReviewRequest

from tests.conftest import GitStand, git_in


def _request(stand: GitStand, commit: str | None = None,
             number: int = 1) -> ReviewRequest:
    return ReviewRequest(repository="owner/name", number=number,
                         head_commit=commit or stand.head_commit,
                         head_repository="owner/name")


def _workspaces(tmp_path: Path) -> tuple[Path, ReviewWorkspaces]:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root, ReviewWorkspaces(root)


def _registrations(checkout: Path) -> list[str]:
    """The names of the worktrees registered in the checkout's clone."""
    worktrees = checkout / ".git" / "worktrees"
    if not worktrees.is_dir():
        return []
    return sorted(entry.name for entry in worktrees.iterdir())


def _sakshi_refs(checkout: Path) -> list[str]:
    """The refs under refs/sakshi/ the checkout holds."""
    return [line for line in
            git_in(checkout, "for-each-ref", "refs/sakshi/").splitlines()
            if line.strip()]


def _case_insensitive(directory: Path) -> bool:
    (directory / "case-probe-a").write_text("", encoding="utf-8")
    twin = directory / "CASE-PROBE-A"
    insensitive = twin.exists()
    (directory / "case-probe-a").unlink()
    if twin.exists():
        twin.unlink()
    return insensitive


def test_prepare_lays_the_tree_and_reviews_whatever_the_umask(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)

    old = os.umask(0o077)
    try:
        workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                               request=_request(git_stand)))
    finally:
        os.umask(old)
    assert workspace.directory.parent == root
    assert workspace.tree == workspace.directory / "tree"
    assert workspace.reviews == workspace.directory / "reviews"
    assert workspace.checkout == git_stand.checkout
    assert stat.S_IMODE(workspace.directory.stat().st_mode) == 0o755
    assert stat.S_IMODE(workspace.reviews.stat().st_mode) == 0o755
    assert stat.S_IMODE(workspace.tree.stat().st_mode) == 0o755
    assert stat.S_IMODE((workspace.tree / "file.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE((workspace.tree / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((workspace.tree / "sub").stat().st_mode) == 0o755
    assert (workspace.tree / "file.txt").read_text(encoding="utf-8") == \
        "the pull request's content\n"
    assert (workspace.tree / "main-only.txt").exists() is False


@pytest.mark.parametrize("bad_checkout", ["sub", "origin.git"])
def test_a_checkout_not_at_the_top_of_its_repository_is_refused(
        git_stand: GitStand, tmp_path: Path, bad_checkout: str) -> None:
    root, spaces = _workspaces(tmp_path)
    checkout = (git_stand.seed / "sub" if bad_checkout == "sub"
                else git_stand.origin)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=checkout,
                                   request=_request(git_stand)))
    assert str(checkout) in str(raised.value)
    assert list(root.iterdir()) == []


def test_a_checkout_reached_through_a_symlink_to_the_top_is_accepted(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    link = tmp_path / "link-to-checkout"
    link.symlink_to(git_stand.checkout)
    workspace = asyncio.run(spaces.prepare(checkout=link,
                                           request=_request(git_stand)))
    assert (workspace.tree / "file.txt").exists()


def test_a_head_commit_absent_from_the_pr_head_is_refused_and_lays_nothing(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    request = _request(git_stand, commit="e" * 40)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=request))
    assert "is not in the pull request's head" in str(raised.value)
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_a_commit_only_on_main_is_refused(git_stand: GitStand,
                                          tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand,
                                                    git_stand.main_commit)))
    assert "is not in the pull request's head" in str(raised.value)
    assert list(root.iterdir()) == []


def test_the_heads_parent_is_accepted(git_stand: GitStand,
                                      tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(
        spaces.prepare(checkout=git_stand.checkout,
                       request=_request(git_stand, git_stand.base_commit)))
    assert (workspace.tree / "file.txt").read_text(encoding="utf-8") == \
        "the first content\n"


def test_a_moved_pr_ref_refuses_the_earlier_head(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    git_in(git_stand.seed, "checkout", "-b", "moved",
           git_stand.base_commit)
    (git_stand.seed / "moved.txt").write_text("an unrelated line\n",
                                              encoding="utf-8")
    git_in(git_stand.seed, "add", "-A")
    git_in(git_stand.seed, "commit", "-m", "the head has moved")
    git_in(git_stand.seed, "push", "--force", str(git_stand.origin),
           "HEAD:refs/pull/1/head")
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert "is not in the pull request's head" in str(raised.value)
    assert list(root.iterdir()) == []


def test_two_concurrent_prepares_on_one_checkout_both_succeed(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)

    async def scenario() -> tuple[Workspace, Workspace]:
        return await asyncio.gather(
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)),
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)))

    one, two = asyncio.run(scenario())
    assert one.directory != two.directory
    assert (one.tree / "file.txt").exists()
    assert (two.tree / "file.txt").exists()
    assert len(list(root.iterdir())) == 2


def test_the_ref_the_membership_check_reads_is_sakshis_own(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetch lays the pull request's head under a ref of Sakshi's
    own, never FETCH_HEAD, and the ref is gone after every way out of
    the check: a pass, a membership refusal, a failed fetch and a
    cancel."""
    recorded: list[list[str]] = []
    real = review_workspace.run_git

    async def recording(arguments: Sequence[str], *, cwd: Path,
                       timeout: float) -> str:
        recorded.append(list(arguments))
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", recording)
    root, spaces = _workspaces(tmp_path)

    workspace = asyncio.run(spaces.prepare(
        checkout=git_stand.checkout, request=_request(git_stand)))
    assert (workspace.tree / "file.txt").exists()
    fetches = [call for call in recorded if list(call[:1]) == ["fetch"]]
    assert len(fetches) == 1
    assert re.fullmatch(
        r"\+refs/pull/1/head:refs/sakshi/review-[0-9a-f]{32}",
        fetches[0][-1])
    assert not any("FETCH_HEAD" in call for call in recorded)
    assert _sakshi_refs(git_stand.checkout) == []
    asyncio.run(spaces.remove(workspace))
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []

    with pytest.raises(ReviewWorkspaceError):
        asyncio.run(spaces.prepare(
            checkout=git_stand.checkout,
            request=_request(git_stand, "e" * 40)))
    assert _sakshi_refs(git_stand.checkout) == []

    with pytest.raises(ReviewWorkspaceError):
        asyncio.run(spaces.prepare(
            checkout=git_stand.checkout,
            request=_request(git_stand, number=9)))
    assert _sakshi_refs(git_stand.checkout) == []
    assert list(root.iterdir()) == []

    started = asyncio.Event()

    async def holding(arguments: Sequence[str], *, cwd: Path,
                      timeout: float) -> str:
        if list(arguments[:1]) == ["fetch"]:
            started.set()
            await asyncio.sleep(30)
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", holding)

    async def cancelled() -> None:
        task = asyncio.create_task(
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled())
    assert _sakshi_refs(git_stand.checkout) == []
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_an_operators_fetch_does_not_move_the_membership_check(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """FETCH_HEAD is the checkout's, and any fetch may move it; the
    check reads a ref of Sakshi's own, which no operator fetch
    touches."""
    root, spaces = _workspaces(tmp_path)
    real = review_workspace.run_git

    async def interfering(arguments: Sequence[str], *, cwd: Path,
                          timeout: float) -> str:
        result = await real(arguments, cwd=cwd, timeout=timeout)
        if list(arguments[:1]) == ["fetch"]:
            git_in(cwd, "fetch", "origin", "refs/heads/main")
        return result

    monkeypatch.setattr(review_workspace, "run_git", interfering)
    workspace = asyncio.run(spaces.prepare(
        checkout=git_stand.checkout, request=_request(git_stand)))
    assert (workspace.tree / "file.txt").exists()
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(
            checkout=git_stand.checkout,
            request=_request(git_stand, git_stand.main_commit)))
    assert "is not in the pull request's head" in str(raised.value)


def test_a_prepare_leaves_the_checkouts_fetch_head_as_it_was(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The fetch names a destination ref of its own and writes no
    FETCH_HEAD, so the checkout's own record of its last fetch is the
    operator's, not the review's."""
    root, spaces = _workspaces(tmp_path)
    git_in(git_stand.checkout, "fetch", "origin", "main")
    fetch_head = git_stand.checkout / ".git" / "FETCH_HEAD"
    before = fetch_head.read_text(encoding="utf-8")
    workspace = asyncio.run(spaces.prepare(
        checkout=git_stand.checkout, request=_request(git_stand)))
    assert fetch_head.read_text(encoding="utf-8") == before
    assert (workspace.tree / "file.txt").exists()
    assert _sakshi_refs(git_stand.checkout) == []


def test_a_cancel_that_lands_in_the_ref_deletion_still_deletes_it(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The deletion is a task of its own, waited out: a cancel that
    lands in it lets it finish before the cancellation surfaces, so the
    ref is gone on every way out of the check — and the prepare is
    still waiting while the deletion holds."""
    root, spaces = _workspaces(tmp_path)
    real = review_workspace.run_git
    at_deletion = asyncio.Event()
    release = asyncio.Event()

    async def holding(arguments: Sequence[str], *, cwd: Path,
                      timeout: float) -> str:
        if list(arguments[:2]) == ["update-ref", "-d"]:
            at_deletion.set()
            await release.wait()
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", holding)

    async def scenario() -> None:
        task = asyncio.create_task(
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)))
        await at_deletion.wait()
        try:
            task.cancel()
            await asyncio.sleep(0.1)
            ended = task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 12.0)
        assert ended is False

    asyncio.run(scenario())
    assert _sakshi_refs(git_stand.checkout) == []


def test_a_ref_that_cannot_be_deleted_is_named_and_the_prepare_goes_on(
        git_stand: GitStand, tmp_path: Path, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A deletion that fails is one line on standard output, in the form
    of the review's other lines, and the review goes on: the tree is
    laid and the ref stays, named for the operator."""
    root, spaces = _workspaces(tmp_path)
    refused: list[str] = []
    real = review_workspace.run_git

    async def refusing(arguments: Sequence[str], *, cwd: Path,
                       timeout: float) -> str:
        if list(arguments[:2]) == ["update-ref", "-d"]:
            refused.append(arguments[-1])
            raise GitError(128, "cannot lock ref")
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", refusing)
    workspace = asyncio.run(spaces.prepare(
        checkout=git_stand.checkout, request=_request(git_stand)))
    assert (workspace.tree / "file.txt").exists()
    [ref] = refused
    names = [line.split("\t")[-1]
             for line in _sakshi_refs(git_stand.checkout)]
    assert names == [ref]
    assert capsys.readouterr().out == (
        f"Review owner/name#1: temporary ref {ref} not deleted: "
        f"GitError\n")


def test_prepares_of_one_checkout_do_not_overlap_their_git_calls(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root, spaces = _workspaces(tmp_path)
    real = review_workspace.run_git
    active = 0
    peak = 0

    async def tracking(arguments: Sequence[str], *, cwd: Path,
                       timeout: float) -> str:
        nonlocal active, peak
        if list(arguments[:2]) == ["worktree", "add"]:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.2)
            try:
                return await real(arguments, cwd=cwd, timeout=timeout)
            finally:
                active -= 1
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", tracking)

    async def scenario() -> tuple[Workspace, Workspace]:
        return await asyncio.gather(
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)),
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)))

    one, two = asyncio.run(scenario())
    assert (one.tree / "file.txt").exists()
    assert (two.tree / "file.txt").exists()
    assert peak == 1


def test_a_root_that_is_not_a_directory_is_the_error(
        git_stand: GitStand, tmp_path: Path) -> None:
    root = tmp_path / "a-file"
    root.write_text("", encoding="utf-8")
    spaces = ReviewWorkspaces(root)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert str(root) in str(raised.value)


def test_a_role_name_that_is_not_a_slug_is_refused(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    with pytest.raises(ValueError):
        spaces.write_reviews(workspace, {"reviewer-2": "ok",
                                         "Not A Slug": "x"})
    assert list(workspace.reviews.iterdir()) == []
    with pytest.raises(ValueError):
        spaces.write_reviews(workspace, {"Not A Slug": "x"})
    assert list(workspace.reviews.iterdir()) == []

    old = os.umask(0o077)
    try:
        spaces.write_reviews(workspace, {"reviewer-1": "# report\n"})
    finally:
        os.umask(old)
    report = workspace.reviews / "reviewer-1.md"
    assert report.read_text(encoding="utf-8") == "# report\n"
    assert stat.S_IMODE(report.stat().st_mode) == 0o644


def _status_refused(git_stand: GitStand, root: Path,
                    spaces: ReviewWorkspaces,
                    monkeypatch: pytest.MonkeyPatch,
                    alter) -> None:
    real = review_workspace.run_git

    async def altering(arguments: Sequence[str], *, cwd: Path,
                       timeout: float) -> str:
        result = await real(arguments, cwd=cwd, timeout=timeout)
        if list(arguments[:2]) == ["worktree", "add"]:
            alter(Path(arguments[3]))
        return result

    monkeypatch.setattr(review_workspace, "run_git", altering)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert "the tree differs from the head commit" in str(raised.value)
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_a_tree_that_differs_from_its_commit_is_refused(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root, spaces = _workspaces(tmp_path)

    def remove_a_tracked_file(tree: Path) -> None:
        (tree / "file.txt").unlink()

    _status_refused(git_stand, root, spaces, monkeypatch,
                    remove_a_tracked_file)


def test_a_case_colliding_pull_request_is_refused(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The colliding commit is built by plumbing, never through a
    working tree: on a case-insensitive file system a working tree
    folds the pair at the commit, leaving nothing to refuse."""
    upper = git_in(git_stand.seed, "hash-object", "-w", "--stdin",
                   input="upper\n").strip()
    lower = git_in(git_stand.seed, "hash-object", "-w", "--stdin",
                   input="lower\n").strip()
    entries = git_in(git_stand.seed, "ls-tree", git_stand.head_commit)
    lines = [line for line in entries.splitlines() if line.strip()]
    lines.append(f"100644 blob {upper}\tCollide.txt")
    lines.append(f"100644 blob {lower}\tcollide.txt")
    tree = git_in(git_stand.seed, "mktree",
                  input="\n".join(lines) + "\n").strip()
    colliding = git_in(git_stand.seed, "commit-tree", tree, "-p",
                       git_stand.head_commit,
                       input="a colliding pair\n").strip()
    git_in(git_stand.seed, "push", str(git_stand.origin),
           f"{colliding}:refs/pull/2/head")
    names = git_in(git_stand.seed, "ls-tree", "--name-only",
                   colliding).split()
    assert "Collide.txt" in names and "collide.txt" in names
    if not _case_insensitive(tmp_path):
        pytest.skip("case-sensitive file system")
    root, spaces = _workspaces(tmp_path)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(
            checkout=git_stand.checkout,
            request=_request(git_stand, colliding, number=2)))
    assert "the tree differs from the head commit" in str(raised.value)
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def _install_hook(checkout: Path, body: str) -> None:
    hook = checkout / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    hook.chmod(0o755)


def test_a_failing_post_checkout_hook_leaves_nothing(
        git_stand: GitStand, tmp_path: Path) -> None:
    """A hook that fails makes the worktree add fail with the
    registration and the directory both left behind, and the cleanup
    takes them apart."""
    root, spaces = _workspaces(tmp_path)
    _install_hook(git_stand.checkout, "exit 1\n")
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert "worktree add failed" in str(raised.value)
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_an_ignored_file_written_by_a_hook_is_refused(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The tree check counts the ignored files too: a tree holding
    anything beyond the head commit is a workspace that could not be
    laid."""
    root, spaces = _workspaces(tmp_path)
    exclude = git_stand.checkout / ".git" / "info" / "exclude"
    with open(exclude, "a", encoding="utf-8") as handle:
        handle.write("*.generated\n")
    _install_hook(git_stand.checkout,
                  "echo payload > payload.generated\n")
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert "the tree differs from the head commit" in str(raised.value)
    assert "!! payload.generated" in str(raised.value)
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


@pytest.mark.parametrize("hook_body, diagnostic", [
    ("echo payload > payload.generated\n", "!! payload.generated"),
    ("echo payload > payload.txt\n", "?? payload.txt"),
])
def test_a_tree_beyond_its_commit_is_refused_whatever_the_setting(
        git_stand: GitStand, tmp_path: Path, hook_body: str,
        diagnostic: str) -> None:
    """The tree check says itself how untracked files are shown, so a
    checkout configured to hide them cannot hide a tree that holds
    more than its commit."""
    root, spaces = _workspaces(tmp_path)
    git_in(git_stand.checkout, "config", "status.showUntrackedFiles",
           "no")
    exclude = git_stand.checkout / ".git" / "info" / "exclude"
    with open(exclude, "a", encoding="utf-8") as handle:
        handle.write("*.generated\n")
    _install_hook(git_stand.checkout, hook_body)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert "the tree differs from the head commit" in str(raised.value)
    assert diagnostic in str(raised.value)
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_a_cancelled_prepare_leaves_no_directory_and_no_registration(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root, spaces = _workspaces(tmp_path)
    real = review_workspace.run_git

    added = asyncio.Event()

    async def waiting(arguments: Sequence[str], *, cwd: Path,
                      timeout: float) -> str:
        result = await real(arguments, cwd=cwd, timeout=timeout)
        if list(arguments[:2]) == ["worktree", "add"]:
            added.set()
            await asyncio.sleep(30)
        return result

    monkeypatch.setattr(review_workspace, "run_git", waiting)

    async def scenario() -> None:
        task = asyncio.create_task(
            spaces.prepare(checkout=git_stand.checkout,
                           request=_request(git_stand)))
        await added.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_remove_leaves_no_directory_and_no_worktree(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []
    assert list(root.iterdir()) == []


def test_remove_does_not_raise_over_an_untracked_and_a_modified_file(
        git_stand: GitStand, tmp_path: Path) -> None:
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    (workspace.tree / "untracked.txt").write_text("new\n",
                                                  encoding="utf-8")
    (workspace.tree / "file.txt").write_text("changed\n",
                                             encoding="utf-8")
    asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []


def test_a_failed_remove_still_deletes_the_directory_and_raises(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first unregistration fails: the second, after the
    directory is gone, lists the tree afresh and ends the
    registration."""
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    real = review_workspace.run_git
    removals = 0

    async def failing(arguments: Sequence[str], *, cwd: Path,
                      timeout: float) -> str:
        nonlocal removals
        if list(arguments[:2]) == ["worktree", "remove"]:
            removals += 1
            if removals == 1:
                raise GitError(128, "boom")
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", failing)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.remove(workspace))
    assert "boom" in str(raised.value)
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []


def test_a_failed_prepare_leaves_no_directory_or_registration(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The worktree add never registers anything, so the cleanup finds
    the tree unlisted and runs no removal."""
    root, spaces = _workspaces(tmp_path)
    real = review_workspace.run_git
    removals: list[list[str]] = []

    async def failing(arguments: Sequence[str], *, cwd: Path,
                      timeout: float) -> str:
        if list(arguments[:2]) == ["worktree", "add"]:
            raise GitError(128, "boom")
        if list(arguments[:2]) == ["worktree", "remove"]:
            removals.append(list(arguments))
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", failing)
    with pytest.raises(ReviewWorkspaceError):
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []
    assert removals == []


def test_a_failure_of_the_directorys_mode_leaves_no_directory(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root, spaces = _workspaces(tmp_path)
    real_chmod = review_workspace.os.chmod

    def failing_chmod(path, mode, **kwargs):
        if Path(path).parent == root:
            raise OSError(1, "mode refused")
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(review_workspace.os, "chmod", failing_chmod)
    with pytest.raises(OSError):
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert list(root.iterdir()) == []
    assert _registrations(git_stand.checkout) == []


def test_remove_deletes_a_tree_left_unwritable(
        git_stand: GitStand, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("the superuser ignores the modes")
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    workspace.tree.chmod(0o500)
    with pytest.raises(ReviewWorkspaceError):
        asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []


def test_a_removal_failing_at_the_directory_keeps_the_roots_mode(
        git_stand: GitStand, tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("the superuser ignores the modes")
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    root.chmod(0o511)
    try:
        with pytest.raises(ReviewWorkspaceError):
            asyncio.run(spaces.remove(workspace))
        assert stat.S_IMODE(root.stat().st_mode) == 0o511
        assert workspace.directory.exists()
    finally:
        root.chmod(0o755)


def test_an_unrelated_registration_is_left_alone(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No prune runs: a worktree of the checkout whose directory was
    renamed away stays registered through a review's prepare and
    remove, and through a failed prepare."""
    mount = tmp_path / "mount"
    mount.mkdir()
    git_in(git_stand.checkout, "worktree", "add", "--detach",
           str(mount / "op"), git_stand.base_commit)
    mount.rename(tmp_path / "away")
    assert _registrations(git_stand.checkout) == ["op"]
    root, spaces = _workspaces(tmp_path)

    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    asyncio.run(spaces.remove(workspace))
    assert _registrations(git_stand.checkout) == ["op"]
    assert list(root.iterdir()) == []

    real = review_workspace.run_git

    async def failing(arguments: Sequence[str], *, cwd: Path,
                      timeout: float) -> str:
        if list(arguments[:2]) == ["worktree", "add"]:
            raise GitError(128, "boom")
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(review_workspace, "run_git", failing)
    with pytest.raises(ReviewWorkspaceError):
        asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                   request=_request(git_stand)))
    assert _registrations(git_stand.checkout) == ["op"]
    assert list(root.iterdir()) == []


def test_a_listed_path_that_cannot_resolve_is_skipped(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A worktree listed before the review's tree whose path cannot
    resolve, a symlink loop on some interpreters, is skipped and the
    review's own tree is still found later in the listing."""
    mount = tmp_path / "mount"
    mount.mkdir()
    git_in(git_stand.checkout, "worktree", "add", "--detach",
           str(mount / "op"), git_stand.base_commit)
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    unrelated = mount / "op"
    real_resolve = Path.resolve

    def refusing(self, *args, **kwargs):
        if self == unrelated:
            raise RuntimeError("symlink loop")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(review_workspace.Path, "resolve", refusing)
    asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == ["op"]


def test_a_locked_registration_does_not_stop_remove(
        git_stand: GitStand, tmp_path: Path) -> None:
    """A registration left locked, as a killed worktree add leaves it,
    is removed by the doubled force."""
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    locked = (git_stand.checkout / ".git" / "worktrees" / "tree"
              / "locked")
    locked.write_text("initializing", encoding="utf-8")
    asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []


def test_a_tree_the_operator_already_removed_is_only_deleted(
        git_stand: GitStand, tmp_path: Path) -> None:
    """A tree unregistered by someone else is not listed, so nothing
    runs and only the directory goes."""
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    git_in(git_stand.checkout, "worktree", "remove", "--force",
           str(workspace.tree))
    asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []


def test_remove_of_a_directory_already_deleted_raises_the_named_failure(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The chmod pass cannot even list a directory that is gone; the
    pass ends, rmtree meets what is left and the failure is named, a
    raw OSError never escaping."""
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    shutil.rmtree(workspace.directory)
    with pytest.raises(ReviewWorkspaceError) as raised:
        asyncio.run(spaces.remove(workspace))
    assert "remove directory failed" in str(raised.value)
    assert _registrations(git_stand.checkout) == []


def test_a_directory_the_chmod_pass_cannot_list_is_left_for_rmtree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    review = tmp_path / "review"
    blocked = review / "blocked"
    sibling = review / "sibling"
    blocked.mkdir(parents=True)
    sibling.mkdir()
    sibling.chmod(0o500)
    real_scandir = review_workspace.os.scandir

    def blocking(path):
        if Path(path) == blocked:
            raise PermissionError(1, "denied")
        return real_scandir(path)

    monkeypatch.setattr(review_workspace.os, "scandir", blocking)
    review_workspace._chmod_directories(review)
    assert stat.S_IMODE(sibling.stat().st_mode) == 0o700


def test_an_entry_whose_lookup_fails_is_left_for_rmtree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError from an entry's own lookup, not only from its chmod,
    leaves that entry for rmtree to meet and the walk goes on."""
    review = tmp_path / "review"
    broken = review / "broken"
    sibling = review / "sibling"
    broken.mkdir(parents=True)
    sibling.mkdir()
    sibling.chmod(0o500)
    real_scandir = review_workspace.os.scandir

    class BrokenEntry:
        path = str(broken)

        def is_dir(self, follow_symlinks: bool = False) -> bool:
            raise OSError(5, "I/O error")

    class Listing:
        """A scandir-shaped listing: an iterator usable as a context
        manager, as os.scandir's result is."""

        def __init__(self, entries: list) -> None:
            self._entries = iter(entries)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

        def __enter__(self):
            return self

        def __exit__(self, *exception) -> None:
            pass

    def wrapping(path):
        if Path(path) != review:
            return real_scandir(path)
        with real_scandir(path) as entries:
            kept = [entry for entry in entries if entry.name != "broken"]
        return Listing([BrokenEntry(), *kept])

    monkeypatch.setattr(review_workspace.os, "scandir", wrapping)
    review_workspace._chmod_directories(review)
    assert stat.S_IMODE(sibling.stat().st_mode) == 0o700


def test_remove_takes_apart_a_tree_with_an_unchmoddable_directory(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The unregistration fails on the unreadable directory, the chmod
    pass opens it and the rmtree deletes it; the failure is still
    named."""
    if os.geteuid() == 0:
        pytest.skip("the superuser ignores the modes")
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    nested = workspace.tree / "nested" / "deep"
    nested.mkdir(parents=True)
    workspace.tree.joinpath("nested").chmod(0o000)
    with pytest.raises(ReviewWorkspaceError):
        asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []


def test_remove_never_follows_a_symlink_out_of_the_review(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The chmod pass enters no symlink: a directory outside the
    review keeps its mode and its file."""
    if os.geteuid() == 0:
        pytest.skip("the superuser ignores the modes")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("kept\n", encoding="utf-8")
    outside.chmod(0o500)
    root, spaces = _workspaces(tmp_path)
    workspace = asyncio.run(spaces.prepare(checkout=git_stand.checkout,
                                           request=_request(git_stand)))
    (workspace.tree / "away").symlink_to(outside)
    asyncio.run(spaces.remove(workspace))
    assert not workspace.directory.exists()
    assert _registrations(git_stand.checkout) == []
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "kept\n"

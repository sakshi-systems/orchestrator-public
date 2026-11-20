"""A review's workspace: the checkout's tree laid outside every seed.

Each accepted review gets a directory of a random name under the
workspace root, holding a detached worktree of the pull request's head
commit and a reviews directory the roles' reports are written into.
The tree is laid from the repository's checkout under one lock per
checkout, so no two prepares or removes race git in one clone; a
failed or cancelled prepare leaves neither a directory nor a worktree
registration behind.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .git_runner import GitError, run_git
from .reviews import ReviewRequest, is_slug
from .waiting import wait_out

_DIRECTORY_MODE = 0o755
_FILE_MODE = 0o644


class ReviewWorkspaceError(Exception):
    """A workspace step that failed; the message names the step and
    git's text."""


@dataclass(frozen=True)
class Workspace:
    """One review's directory: the tree under it, the reviews folder
    beside it, and the checkout it was laid from."""

    directory: Path
    tree: Path
    reviews: Path
    checkout: Path


def _chmod_directories(directory: Path) -> None:
    """Make every real directory strictly below the review's directory
    traversable, from the top down: each directory found by a listing
    is given 0o700 before it is listed itself, so a directory of mode
    000 can be read before it is walked.  A symlink is neither entered
    nor chmodded, so no link is ever followed into something Sakshi
    does not own; files are not touched, and the review's own
    directory and everything above it keep their modes.  An OSError,
    on a listing, on an entry's lookup or on its chmod, leaves that
    entry's subtree alone for ``rmtree`` to meet."""
    def walk(current: Path) -> None:
        try:
            with os.scandir(current) as entries:
                children = list(entries)
        except OSError:
            return
        for entry in children:
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_directory:
                continue
            child = Path(entry.path)
            try:
                os.chmod(child, 0o700)
            except OSError:
                continue
            walk(child)

    walk(directory)


def _write_report(path: Path, report: str) -> None:
    """Write one report by atomic replace, UTF-8, readable by group and
    others whatever Sakshi's umask."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(report)
        os.chmod(temporary, _FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class ReviewWorkspaces:
    """Lays, fills and removes review workspaces under a root."""

    def __init__(self, root: Path, *, timeout: float = 300.0) -> None:
        self._root = root
        self._timeout = timeout
        self._locks: dict[Path, asyncio.Lock] = {}

    def _lock_for(self, checkout: Path) -> asyncio.Lock:
        resolved = checkout.resolve()
        lock = self._locks.get(resolved)
        if lock is None:
            lock = self._locks[resolved] = asyncio.Lock()
        return lock

    async def _git(self, arguments, cwd: Path) -> str:
        return await run_git(arguments, cwd=cwd, timeout=self._timeout)

    async def prepare(self, *, checkout: Path,
                      request: ReviewRequest) -> Workspace:
        """Lay one review's workspace from the repository's checkout.

        The checkout must be the top of its repository; the pull
        request's head is fetched and the request's head commit must be
        an ancestor of the fetched head, the head itself included; a
        detached worktree of the commit and a reviews directory are
        laid under a fresh directory of the root, and the tree must
        equal its commit.  A failed or cancelled prepare leaves neither
        a directory nor a worktree registration.
        """
        if not self._root.is_dir():
            raise ReviewWorkspaceError(
                f"workspace root {self._root} is not a directory")
        async with self._lock_for(checkout):
            return await self._prepare(checkout, request)

    async def _prepare(self, checkout: Path,
                       request: ReviewRequest) -> Workspace:
        try:
            toplevel = (await self._git(
                ["rev-parse", "--show-toplevel"], checkout)).strip()
        except GitError:
            raise ReviewWorkspaceError(
                f"checkout {checkout} is not the top of a repository"
            ) from None
        if Path(toplevel).resolve() != checkout.resolve():
            raise ReviewWorkspaceError(
                f"checkout {checkout} is not the top of a repository")
        ref = f"refs/sakshi/review-{uuid.uuid4().hex}"
        try:
            try:
                await self._git(
                    ["fetch", "--no-tags", "--no-write-fetch-head",
                     "--quiet", "origin",
                     f"+refs/pull/{request.number}/head:{ref}"],
                    checkout)
            except GitError as e:
                raise ReviewWorkspaceError(f"fetch failed: {e.text}") \
                    from None
            try:
                await self._git(
                    ["merge-base", "--is-ancestor", request.head_commit,
                     ref],
                    checkout)
            except GitError:
                raise ReviewWorkspaceError(
                    "membership failed: the head commit "
                    f"{request.head_commit} is not in the pull request's "
                    "head"
                ) from None
        finally:
            deletion = asyncio.ensure_future(
                self._delete_ref(checkout, ref, request))
            await wait_out(deletion)
        directory = self._root / uuid.uuid4().hex
        try:
            os.mkdir(directory)
            os.chmod(directory, _DIRECTORY_MODE)
            try:
                await self._git(
                    ["worktree", "add", "--detach",
                     str(directory / "tree"), request.head_commit],
                    checkout)
            except GitError as e:
                raise ReviewWorkspaceError(
                    f"worktree add failed: {e.text}") from None
            reviews = directory / "reviews"
            os.mkdir(reviews)
            os.chmod(reviews, _DIRECTORY_MODE)
            try:
                status = await self._git(
                    ["-C", str(directory / "tree"), "status", "--porcelain",
                     "--ignored", "--untracked-files=normal"],
                    checkout)
            except GitError as e:
                raise ReviewWorkspaceError(
                    f"status failed: {e.text}") from None
            if status.strip():
                raise ReviewWorkspaceError(
                    "status failed: the tree differs from the head commit: "
                    f"{status.strip()[:500]}")
        except BaseException:
            await self._cleanup(checkout, directory)
            raise
        return Workspace(directory=directory, tree=directory / "tree",
                         reviews=reviews, checkout=checkout)

    async def _delete_ref(self, checkout: Path, ref: str,
                          request: ReviewRequest) -> None:
        """Delete the review's temporary ref, a waited task that catches
        its own failure: a deletion that failed prints one line naming
        the review, the ref and the error's type, and the ref stays for
        the operator to see."""
        try:
            await self._git(["update-ref", "-d", ref], checkout)
        except GitError as error:
            print(f"Review {request.repository}#{request.number}: "
                  f"temporary ref {ref} not deleted: "
                  f"{type(error).__name__}")

    async def _unregister_tree(self, checkout: Path,
                               directory: Path) -> None:
        """Remove the checkout's registration of the review's tree.

        The removal runs only when the checkout's worktree list names
        the tree in a ``worktree <path>`` line, the listed path and the
        tree compared resolved; a listed path that cannot resolve, a
        symlink loop among them, is skipped, and a tree that is not
        listed is left alone and no removal runs.  A failure of the
        listing, or of the removal of a listed tree, is raised.
        """
        tree = directory / "tree"
        resolved_tree = tree.resolve()
        listing = await self._git(["worktree", "list", "--porcelain"],
                                  checkout)
        for line in listing.splitlines():
            if not line.startswith("worktree "):
                continue
            listed = Path(line[len("worktree "):].strip())
            try:
                resolved = listed.resolve()
            except (RuntimeError, OSError):
                continue
            if resolved == resolved_tree:
                await self._git(
                    ["worktree", "remove", "--force", "--force", str(tree)],
                    checkout)
                return

    async def _take_apart(self, checkout: Path, directory: Path) -> None:
        """Unregister the review's tree, delete the review's directory,
        and, when the first unregistration failed, unregister again:
        the tree is then listed afresh and removed only if it is still
        listed, the directory being gone by then.  The first failure is
        the one named; the directory is deleted in every case."""
        failure: str | None = None
        unregistered = False
        try:
            await self._unregister_tree(checkout, directory)
            unregistered = True
        except GitError as e:
            failure = f"worktree remove failed: {e.text}"
        _chmod_directories(directory)
        try:
            shutil.rmtree(directory)
        except OSError as e:
            if failure is None:
                failure = f"remove directory failed: {e}"
        if not unregistered:
            try:
                await self._unregister_tree(checkout, directory)
            except GitError:
                pass
        if failure is not None:
            raise ReviewWorkspaceError(failure)

    async def _cleanup(self, checkout: Path, directory: Path) -> None:
        """Remove a failed prepare's registration and directory,
        swallowing every failure so the failure that began the cleanup
        stands."""
        try:
            await self._take_apart(checkout, directory)
        except (OSError, GitError, ReviewWorkspaceError):
            pass

    def write_reviews(self, workspace: Workspace,
                      reports: Mapping[str, str]) -> None:
        """Write one ``<role>.md`` per report into the workspace's
        reviews directory, UTF-8, 0644, by atomic replace.  A key that
        is not a slug is a ``ValueError``, and one invalid name among
        the reports writes nothing."""
        for role in reports:
            if not is_slug(role):
                raise ValueError(f"role name is not a slug: {role!r}")
        for role, report in reports.items():
            _write_report(workspace.reviews / f"{role}.md", report)

    async def remove(self, workspace: Workspace) -> None:
        """Remove one review's workspace: the worktree unregistered,
        the directory deleted, and, when the first unregistration
        failed, unregistered again after the directory is gone.  A
        failed step still deletes the directory and raises."""
        async with self._lock_for(workspace.checkout):
            await self._take_apart(workspace.checkout,
                                   workspace.directory)

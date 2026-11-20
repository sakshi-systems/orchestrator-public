"""Running git for a review's workspace: one process group per call,
ended on time or on cancel, laying files readable by everyone.

Sakshi runs git in a repository's checkout to fetch a pull request's
head and to lay a review's tree.  Each call is a session of its own,
so a call past its time, or a cancelled call, can end every process
git began, git itself removing its lock files on the TERM; and the
files and directories a call creates are readable by group and others
whatever Sakshi's umask, because a review's tree is read by a role
user, not by Sakshi.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Sequence
from pathlib import Path

_DETAIL_LIMIT = 500
_KILL_GRACE = 5.0
_TIMEOUT_RETURNCODE = 124

GIT_TERMINAL_PROMPT = "0"

# Git's own repository-local names, less the three configuration
# channels, which stay because they may carry the stand's credential
# configuration.
REPOSITORY_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
)


class GitError(Exception):
    """A git call that failed, was ended by a signal, timed out or
    could not run.

    The message names git's exit status, then ": " and its text; the
    text is cut at the detail limit, since git's own errors run long.
    """

    def __init__(self, returncode: int, text: str = "") -> None:
        self.returncode = returncode
        self.text = text
        if text:
            super().__init__(f"git exited {returncode}: {text}")
        else:
            super().__init__(f"git exited {returncode}")


async def _end_group(proc: asyncio.subprocess.Process) -> None:
    """End git's whole group.

    SIGTERM goes to the group first, which lets git remove its lock
    files.  Once git itself has ended, or after the grace seconds, the
    group is probed: no live member answers with ProcessLookupError or
    PermissionError alike (a group of only zombies on macOS answers
    EPERM), and any other answer is a live member, which gets SIGKILL;
    git is then waited for.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), _KILL_GRACE)
    except TimeoutError:
        pass
    try:
        os.killpg(proc.pid, 0)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


async def _end_group_shielded(proc: asyncio.subprocess.Process) -> None:
    """End git's group, kept running to its end under cancellation: a
    cancel while the ending runs waits for the ending to finish, so the
    SIGKILL is not skipped, and the cancellation surfaces after it —
    in place of whatever error began the ending."""
    ending = asyncio.ensure_future(_end_group(proc))
    cancelled = False
    while True:
        try:
            await asyncio.shield(ending)
            break
        except asyncio.CancelledError:
            cancelled = True
            if ending.done():
                raise
    if cancelled:
        raise asyncio.CancelledError


async def run_git(arguments: Sequence[str], *, cwd: Path,
                  timeout: float) -> str:
    """Run ``git <arguments>`` in ``cwd`` and return its output, its
    standard error merged into it.

    The call runs in a session of its own with Sakshi's own
    environment without the names in ``REPOSITORY_LOCATION_VARIABLES``
    — so git uses the repository it is aimed at, never one a hook's
    environment points at — plus ``GIT_TERMINAL_PROMPT=0``, so git
    never waits for a credential no one will type, and with the umask
    022, so whatever Sakshi's umask the files it lays are readable by
    group and others.  The whole output and the exit are read under
    one deadline: a non-zero exit, a signal's negative status among
    them, raises ``GitError(returncode, text)`` with the text cut at
    500 characters, and a call past its timeout raises
    ``GitError(124, ...)``; a call past its timeout, and a cancelled
    call, end git's whole group, shielded from a second cancel, and
    leave no process behind.
    """
    environment = {name: value for name, value in os.environ.items()
                   if name not in REPOSITORY_LOCATION_VARIABLES}
    environment["GIT_TERMINAL_PROMPT"] = GIT_TERMINAL_PROMPT
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *arguments,
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            umask=0o022,
        )
    except OSError as e:
        raise GitError(127, str(e)[:_DETAIL_LIMIT]) from None
    assert proc.stdout is not None
    stdout = proc.stdout

    async def read_and_wait() -> tuple[bytes, int]:
        output = await stdout.read()
        returncode = await proc.wait()
        return output, returncode

    try:
        output, returncode = await asyncio.wait_for(read_and_wait(),
                                                    timeout)
    except TimeoutError:
        await _end_group_shielded(proc)
        raise GitError(_TIMEOUT_RETURNCODE,
                       f"timed out after {timeout} seconds") from None
    except asyncio.CancelledError:
        await _end_group_shielded(proc)
        raise
    text = output.decode("utf-8", "replace")
    if returncode != 0:
        raise GitError(returncode, text[:_DETAIL_LIMIT])
    return text

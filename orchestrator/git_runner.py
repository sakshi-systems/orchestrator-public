"""Running git for a review's workspace and for the run's own adapters:
one process group per call for the review, ended on time or on cancel,
laying files readable by everyone; one synchronous call for the run
that ends, when its bound passes, every process git began, judging no
exit status itself.

Sakshi runs git in a repository's checkout to fetch a pull request's
head and to lay a review's tree.  Each call is a session of its own,
so a call past its time, or a cancelled call, can end every process
git began, git itself removing its lock files on the TERM; and the
files and directories a call creates are readable by group and others
whatever Sakshi's umask, because a review's tree is read by a role
user, not by Sakshi.

The run's adapters — the hosted role's reach and the implementer's
branch — call git synchronously through ``run_git_sync``: bound as the
caller names it, with the terminal prompt off, with every pathspec
literal, and with the exit status left to the caller to judge.
"""

from __future__ import annotations

import asyncio
import locale
import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path

_DETAIL_LIMIT = 500
_KILL_GRACE = 5.0
_TIMEOUT_RETURNCODE = 124

GIT_TERMINAL_PROMPT = "0"

SYNC_GIT_TIMEOUT = 120


class _DefaultBound:
    """The bound a synchronous call takes when its caller names none:
    the module's ``SYNC_GIT_TIMEOUT``, read when the call runs, never
    the constant's value bound at the definition."""

    def __repr__(self) -> str:
        return "SYNC_GIT_TIMEOUT, read when the call runs"


_DEFAULT_BOUND = _DefaultBound()


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


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close git's captured pipes without reading them.

    The ending's reach is git's group; a process that left the group
    under its own session survives it holding an inherited write end,
    and a read to that pipe's end would wait for that process's
    lifetime.  Closing the read ends drops what the call holds — git's
    own output is not read past the raise.
    """
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()


def _end_group_sync(proc: subprocess.Popen) -> None:
    """End git's whole group, the synchronous twin of ``_end_group``:
    the same steps over a waited ``Popen``, sharing the grace.

    SIGTERM goes to the group first, which lets git remove its lock
    files.  Once git itself has ended, or after the grace seconds, the
    group is probed: no live member answers with ProcessLookupError or
    PermissionError alike (a group of only zombies on macOS answers
    EPERM), and any other answer is a live member, which gets SIGKILL.
    Git is then reaped and its captured pipes are closed without being
    read — on every way out of the ending — so no pipe stays held:
    neither by a member that is gone nor by a process that left the
    group under its own session and holds one, whose lifetime a read
    to the pipe's end would wait for, past the call's bound.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(_KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, 0)
    except (ProcessLookupError, PermissionError):
        _close_pipes(proc)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()
    _close_pipes(proc)


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


def _as_text(output: bytes, errors: str) -> str:
    """Decode captured output with the locale's encoding, as the text mode
    of ``subprocess`` does, but without its newline translation: a carriage
    return git printed — a worktree path ending in one among them — is kept
    as git printed it.  The error policy comes with the bytes, so decoding
    never raises: the standard output is read as paths and ref names that
    travel back to git as arguments, and ``surrogateescape`` keeps a byte
    the locale cannot decode as a lone surrogate that ``os.fsencode`` turns
    into the same byte again; the standard error is read only by a person,
    and ``backslashreplace`` keeps its undecodable bytes visible, as
    ``\\xe9``, in a text that never fails to print."""
    return output.decode(locale.getpreferredencoding(False), errors)


def run_git_sync(arguments: Sequence[str], *, cwd: str | Path,
                 timeout: float | None | _DefaultBound = _DEFAULT_BOUND,
                 capture: bool = True,
                 ) -> subprocess.CompletedProcess:
    """Run ``git <arguments>`` synchronously in ``cwd`` and return the
    completed process, judging no exit status: each caller decides what
    a non-zero exit means.

    The call runs with the inherited environment, less the names in
    ``REPOSITORY_LOCATION_VARIABLES`` — so git uses the repository it is
    aimed at in ``cwd``, never one an ambient variable points at — plus
    ``GIT_TERMINAL_PROMPT`` as the module holds it, so git never waits
    for a credential no one will type, and ``GIT_LITERAL_PATHSPECS=1``,
    so a path a caller names is a literal path and never a pattern;
    with the standard input closed; with the standard output and the
    standard error each captured and decoded as text by ``_as_text`` —
    the standard output so a path or a ref name keeps its bytes, the
    standard error so its text stays printable — or, for a caller that
    reads none of git's output, left uncaptured
    and inherited as the caller's own through ``capture=False`` — git's
    text then reaches the console as it runs, and the wait is on git
    alone, so a background job a hook leaves holds no pipe the call
    waits for; and in a session of its own, so the call is the only
    member of its process group beside what git itself begins.  The
    call waits under ``timeout`` seconds — left out, the module's
    ``SYNC_GIT_TIMEOUT`` read when the call runs; ``None``, no bound at
    all, so a caller whose duration is not the network's runs to git's
    own end.  A call past its bound ends git's whole group — every
    process it began, an ssh command or a hook among them — and raises
    ``subprocess.TimeoutExpired``; any other interruption of the wait, a
    ``KeyboardInterrupt`` among them, ends the group the same way and
    propagates as it is.
    """
    environment = {
        name: value for name, value in os.environ.items()
        if name not in REPOSITORY_LOCATION_VARIABLES
    }
    environment["GIT_TERMINAL_PROMPT"] = GIT_TERMINAL_PROMPT
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    bound: float | None
    if isinstance(timeout, _DefaultBound):
        bound = SYNC_GIT_TIMEOUT
    else:
        bound = timeout
    proc = subprocess.Popen(
        ["git", *arguments],
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        start_new_session=True,
    )
    try:
        if capture:
            stdout, stderr = proc.communicate(timeout=bound)
        else:
            proc.wait(timeout=bound)
            return subprocess.CompletedProcess(proc.args, proc.returncode)
    except subprocess.TimeoutExpired as error:
        _end_group_sync(proc)
        raise subprocess.TimeoutExpired(error.cmd, error.timeout)
    except BaseException:
        _end_group_sync(proc)
        raise
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        _as_text(stdout, "surrogateescape"),
        _as_text(stderr, "backslashreplace"))


def _paths_of(result: subprocess.CompletedProcess) -> set[str]:
    """The NUL-separated paths git printed, empty where it printed none."""
    return {path for path in result.stdout.split("\0") if path}


def _changed_paths(cwd: Path, base: str, tip: str) -> set[str]:
    """The paths that differ between the two commits, relative to `cwd`, a
    rename listed as the deletion of its source and the addition of its
    destination whatever the repository's `diff.renames` setting says."""
    diff = run_git_sync(
        ["diff", "--no-renames", "--name-only", "-z", "--relative", base, tip], cwd=cwd,
    )
    return _paths_of(diff)


def _working_changes(cwd: Path, base: str) -> set[str]:
    """The paths of the working tree and the index that differ from the base
    commit, relative to `cwd`: a path the working tree holds or the index
    stages with other content than the base, or holds and the base lacks, or
    the base holds and neither does."""
    working = run_git_sync(
        ["diff", "--no-renames", "--name-only", "-z", "--relative", base], cwd=cwd,
    )
    staged = run_git_sync(
        ["diff", "--no-renames", "--name-only", "-z", "--relative", "--cached", base], cwd=cwd,
    )
    return _paths_of(working) | _paths_of(staged)


def _untracked_paths(cwd: Path) -> set[str]:
    """The untracked files git itself lists — untracked and not ignored, as
    ``git ls-files --others --exclude-standard`` lists them, relative to
    `cwd`; an ignored path never enters."""
    listed = run_git_sync(["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd)
    return _paths_of(listed)

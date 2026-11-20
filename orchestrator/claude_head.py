"""A Claude head runtime: one hosted head's turn as one `claude -p --resume`
call and one fresh session as one `claude -p --session-id` call, with no
retry — a lost outcome is never resubmitted. Turns of one session never
overlap, and a stop ends the running ones; no way out of a turn or a stop
leaves a child behind."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
from collections.abc import Sequence
from pathlib import Path

import sys

from .claude_cli import (
    TURN_DISALLOWED_TOOLS,
    TURN_ENVIRONMENT,
    _classify_result,
    _resolve_claude,
    is_session_id,
    session_start_directory,
)
from .heads import HeadRuntimeProtocol, HeadTurn, HeadTurnError, HeadTurnFailure
from .reviews import is_user_name

# asyncio's reader limit: a single stream line may reach 64 MiB before the
# read fails and the turn's outcome is unknown.
_LINE_LIMIT = 64 * 1024 * 1024
_DETAIL_LIMIT = 500
# seconds a child is given to end on SIGTERM before its group is SIGKILLed;
# decided, not measured
_CLOSE_GRACE = 10.0
# seconds beyond the grace that a stop still waits for a child's ending
_CLOSE_MARGIN = 5.0
# the stand's sudoers rule names these; a role is started through sudo
# and its group ended as the role through /bin/kill
SUDO = "/usr/bin/sudo"
KILL = "/bin/kill"


class ClaudeHeadRuntime:
    """Continues an existing Claude session by its id, or begins a
    fresh one under a caller-chosen id, one call at a time."""

    def __init__(self, executable: str | None = None,
                 environment_names: Sequence[str] = (),
                 *, sudo: str = SUDO, kill: str = KILL) -> None:
        self._executable = executable if executable is not None else _resolve_claude()
        # the names of Sakshi's own environment a fresh session is
        # given; a turn's child keeps the whole environment
        self._environment_names = tuple(environment_names)
        # the two keywords exist for the tests' fakes
        self._sudo = sudo
        self._kill = kill
        self._locks: dict[str, asyncio.Lock] = {}
        self._spawns: set[asyncio.Task[asyncio.subprocess.Process]] = set()
        self._running: set[asyncio.subprocess.Process] = set()
        self._endings: set[asyncio.Task[None]] = set()
        self._closed = False

    async def turn(self, *, session_id: str, cwd: Path, prompt: str) -> HeadTurn:
        # the id first: an invalid one must not grow the lock table
        if not is_session_id(session_id):
            raise HeadTurnError(HeadTurnFailure.INVALID_SESSION)
        if self._closed:
            raise HeadTurnError(HeadTurnFailure.STOPPING)
        # one FIFO lock per session id: a second turn of the session waits
        # in call order, turns of other sessions run together
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if self._closed:
                raise HeadTurnError(HeadTurnFailure.STOPPING)
            return await self._turn(session_id, cwd, prompt)

    async def start(self, *, session_id: str, cwd: Path, prompt: str,
                    model: str, effort: str,
                    add_dirs: Sequence[Path] = (),
                    user: str | None = None) -> HeadTurn:
        """Begin a session under an id the caller chose, running in the
        seed's own directory with the role's model and effort, the extra
        directories added to it and only the named environment given.
        A user, when one is given, starts the session as that OS user,
        through sudo and with its group ended as that user. The id is
        new, so nothing is looked up and SESSION_NOT_SAVED_HERE is
        never raised; the lock table is not used, for a fresh id has no
        concurrent turn and an id per review would grow it for the
        runtime's life."""
        if not is_session_id(session_id):
            raise HeadTurnError(HeadTurnFailure.INVALID_SESSION)
        if self._closed:
            raise HeadTurnError(HeadTurnFailure.STOPPING)
        if user is not None and not is_user_name(user):
            # before any process: a name that cannot be an OS user
            # never reaches sudo
            raise ValueError(f"user is not a user name: {user!r}")
        resolved = cwd.resolve()
        if not resolved.is_dir():
            raise HeadTurnError(HeadTurnFailure.TURN_FAILED,
                                "working directory missing")
        argv = [
            self._executable,
            "-p",
            "--session-id", session_id,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--disallowedTools", TURN_DISALLOWED_TOOLS,
            "--model", model,
            "--effort", effort,
        ]
        for directory in add_dirs:
            argv += ["--add-dir", str(directory)]
        argv += ["--", prompt]
        if user is not None:
            # sudo leads the child's session, so its pid is the group's
            # id, and the group is ended as the role's user
            argv = [self._sudo, "-n", "-u", user, "-H", "--", *argv]
        environment = {name: os.environ[name]
                       for name in self._environment_names
                       if name in os.environ}
        environment.update(TURN_ENVIRONMENT)
        return await self._run_child(argv, resolved, session_id,
                                     environment, user)

    async def aclose(self) -> None:
        """Mark the runtime closed and end every running child's whole
        process group, all at once, waiting for every spawn still in
        flight and every ending already started; the stop costs about
        one grace. Every spawn and ending is awaited through a shield,
        so a cancellation of this close ends none of them, and no
        exception of theirs leaves here. Idempotent."""
        self._closed = True
        while True:
            pending: set[asyncio.Future] = set()
            for spawn in list(self._spawns):
                if not spawn.done():
                    pending.add(asyncio.shield(spawn))
            for proc in list(self._running):
                proc.head_stopped = True  # type: ignore[attr-defined]
                ending = self._ending_for(proc)
                if not ending.done():
                    pending.add(asyncio.shield(ending))
            for ending in list(self._endings):
                if not ending.done():
                    pending.add(asyncio.shield(ending))
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def _ending_for(self, proc: asyncio.subprocess.Process) -> asyncio.Task[None]:
        """The one ending task of this child, started by its first caller;
        every caller — the turn's own exit, a repeated cancellation, a
        stop — awaits the same ending. It removes itself from _endings
        when it is done, so a later close finds an ending still in
        flight and nothing else."""
        ending = getattr(proc, "head_ending", None)
        if ending is None:
            ending = asyncio.ensure_future(self._end_group(proc))
            proc.head_ending = ending  # type: ignore[attr-defined]
            self._endings.add(ending)
            # the done callback is the only removal: _endings holds the
            # endings in flight and nothing else
            ending.add_done_callback(self._endings.discard)
        return ending

    async def _finish_child(self, proc: asyncio.subprocess.Process) -> None:
        """Await this child's ending, starting it when this is its first
        caller; the wait absorbs a repeated cancellation and gives up,
        leaving the ending to finish on its own, only after the grace and a
        margin."""
        ending = self._ending_for(proc)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CLOSE_GRACE + _CLOSE_MARGIN
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(ending),
                                              deadline - loop.time())
            except asyncio.TimeoutError:
                return None
            except asyncio.CancelledError:
                if ending.done():
                    return ending.result()

    async def _end_group(self, proc: asyncio.subprocess.Process) -> None:
        user = getattr(proc, "head_user", None)
        if user is not None:
            await self._end_group_as_user(user, proc)
            return
        self._signal_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLOSE_GRACE)
        except asyncio.TimeoutError:
            pass
        # whether or not the child exited: a grandchild may ignore SIGTERM
        self._signal_group(proc.pid, signal.SIGKILL)
        await proc.wait()

    async def _end_group_as_user(self, user: str,
                                 proc: asyncio.subprocess.Process) -> None:
        """End a role's group as the role's user.

        Sakshi's user may signal only the root-owned sudo, never the
        role's processes, so each signal is delivered by sudo as the
        role's user and then to sudo itself. After the SIGKILL the wait
        for sudo, which reaps it, is bounded by _finish_child's margin;
        only a reaped sudo is probed, and a group that still answers is
        named on standard error and left, for only the role's user may
        end it.
        """
        await self._signal_as_user(user, proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLOSE_GRACE)
        except asyncio.TimeoutError:
            pass
        await self._signal_as_user(user, proc.pid, signal.SIGKILL)
        reaped = True
        try:
            await asyncio.wait_for(proc.wait(),
                                   timeout=_CLOSE_GRACE + _CLOSE_MARGIN)
        except asyncio.TimeoutError:
            reaped = False
        if reaped:
            try:
                # a group id is released when its last process ends, so
                # the probe is safe only here, after the reaping
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                # the group id is gone: nothing of the role is left
                return
            except OSError:
                # any other answer — a permission error among them, the
                # platform naming a group held by another user — is
                # read as a group that lives
                pass
        print(f"a role's group still lives: user {user}, "
              f"group {proc.pid}", file=sys.stderr)

    async def _signal_as_user(self, user: str, pid: int,
                              sig: signal.Signals) -> None:
        """Deliver one signal to a role's process group as the role's
        user, then to sudo itself. The kill run as the user is waited
        for but its exit status is not read, since a group already gone
        makes it exit 1; a lookup or permission error on sudo's own pid
        is ignored, for sudo may already have ended."""
        name = "TERM" if sig is signal.SIGTERM else "KILL"
        try:
            killer = await asyncio.create_subprocess_exec(
                self._sudo, "-n", "-u", user, "-H", "--",
                self._kill, f"-{name}", "--", f"-{pid}",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            killer = None
        if killer is not None:
            try:
                await asyncio.wait_for(killer.wait(),
                                       timeout=_CLOSE_GRACE)
            except asyncio.TimeoutError:
                pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        # the child leads its own session, so its process group is its pid
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    async def _turn(self, session_id: str, cwd: Path, prompt: str) -> HeadTurn:
        resolved = cwd.resolve()
        if not resolved.is_dir():
            # a moved or deleted directory is not a missing executable
            raise HeadTurnError(HeadTurnFailure.SESSION_NOT_SAVED_HERE)
        start = session_start_directory(session_id)
        if start is None or start.resolve() != resolved:
            raise HeadTurnError(HeadTurnFailure.SESSION_NOT_SAVED_HERE)

        argv = [
            self._executable,
            "-p",
            "--resume", session_id,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--disallowedTools", TURN_DISALLOWED_TOOLS,
            "--",
            prompt,
        ]
        return await self._run_child(argv, resolved, session_id,
                                     {**os.environ, **TURN_ENVIRONMENT})

    async def _spawn(self, argv: list[str], directory: Path,
                     environment: dict[str, str],
                     user: str | None) -> asyncio.subprocess.Process:
        """Create the child of one turn or start. The runtime holds this
        task in its set of spawns from before it is awaited until it
        ends, and the child is named in _running before the task
        finishes — no moment exists in which a child lives that neither
        the spawns nor _running name. An OSError is the turn's, mapped
        here once for every way a child begins."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(directory),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=_LINE_LIMIT,
            )
        except OSError as e:
            if e.errno == errno.E2BIG:
                # the prompt travels in argv, so a prompt too long for
                # the argument list is a failed turn, not a missing CLI
                raise HeadTurnError(HeadTurnFailure.TURN_FAILED,
                                    "prompt too long") from None
            raise HeadTurnError(HeadTurnFailure.CLI_UNAVAILABLE) from None
        proc.head_user = user  # type: ignore[attr-defined]
        self._running.add(proc)
        return proc

    async def _run_child(self, argv: list[str], directory: Path,
                         session_id: str, environment: dict[str, str],
                         user: str | None = None) -> HeadTurn:
        """Run one claude child from its argv and environment in the
        directory: read its stream, classify its result and end its
        group on every way out — as the role's user when one was
        given. The spawn is a task the runtime holds, and the turn
        awaits it under a shield: a cancellation of the turn never
        cancels the spawn, but waits for the child it yields and ends
        it before the cancellation is raised."""
        spawn = asyncio.ensure_future(
            self._spawn(argv, directory, environment, user))
        self._spawns.add(spawn)
        spawn.add_done_callback(self._spawns.discard)
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError as cancel:
            # the shield keeps the spawn running; wait for its end —
            # absorbing a repeated cancellation, as the wait for an
            # ending absorbs one — so a child it yields is never left
            # behind, and a spawn that fails raises the cancellation in
            # place of whatever error it ended with. A spawn that was
            # itself cancelled — the loop's end takes every task — can
            # yield nothing, so the wait ends at once rather than spin
            # on a shield that raises without ever suspending
            while True:
                try:
                    proc = await asyncio.shield(spawn)
                    break
                except asyncio.CancelledError:
                    if spawn.cancelled():
                        raise cancel from None
                    continue
                except BaseException:
                    raise cancel from None
            # the child the spawn yielded takes the turn's existing way
            # out — discarded from _running, ended and reaped — before
            # the cancellation is raised
            self._running.discard(proc)
            await self._finish_child(proc)
            raise cancel
        try:
            if self._closed:
                # the stop raced the spawn and saw no child
                proc.head_stopped = True  # type: ignore[attr-defined]
                raise HeadTurnError(HeadTurnFailure.OUTCOME_UNKNOWN)
            final: dict | None = None
            sid_seen = ""
            saw_event = False
            plain: list[str] = []
            # the characters the held plain lines join to; a line that
            # starts at or past the detail limit cannot change the
            # failure's detail, so it is dropped the moment it is read,
            # and a line is held only to the limit's length of itself
            plain_length = 0
            assert proc.stdout is not None
            try:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        if plain_length < _DETAIL_LIMIT:
                            if plain:
                                plain_length += 1
                            kept = line[:_DETAIL_LIMIT]
                            plain.append(kept)
                            plain_length += len(kept)
                        continue
                    if not isinstance(event, dict):
                        if plain_length < _DETAIL_LIMIT:
                            if plain:
                                plain_length += 1
                            kept = line[:_DETAIL_LIMIT]
                            plain.append(kept)
                            plain_length += len(kept)
                        continue
                    saw_event = True
                    if event.get("type") == "result":
                        final = event
                    sid = event.get("session_id")
                    if not sid_seen and isinstance(sid, str) and sid:
                        sid_seen = sid
            except ValueError:
                # a line beyond the stream limit: the turn may have run
                raise HeadTurnError(HeadTurnFailure.OUTCOME_UNKNOWN) from None
            returncode = await proc.wait()
            if getattr(proc, "head_stopped", False):
                # ended by a stop, not by the CLI: the outcome is not known
                raise HeadTurnError(HeadTurnFailure.OUTCOME_UNKNOWN)

            if final is None:
                if saw_event or returncode < 0:
                    # an event without a result, or a child ended by a
                    # signal rather than by the CLI: the turn may have
                    # run, and an unknown outcome is never resubmitted
                    raise HeadTurnError(HeadTurnFailure.OUTCOME_UNKNOWN)
                # no event and a clean end: no session began, so nothing
                # ran; the plain text the CLI wrote, its standard error
                # merged into the stream, is the failure's detail
                raise HeadTurnError(HeadTurnFailure.TURN_FAILED,
                                    "\n".join(plain)[:_DETAIL_LIMIT])

            text = final.get("result")
            if not isinstance(text, str):
                text = ""
            if not text and isinstance(final.get("errors"), list):
                text = "; ".join(str(error) for error in final["errors"])
            detail = text[:_DETAIL_LIMIT]
            is_error = final.get("is_error", False)
            # one attempt of one, so the classifier can never answer "retry"
            verdict = _classify_result(final, text, returncode, is_error, 1, 1)
            if verdict == "ratelimit":
                raise HeadTurnError(HeadTurnFailure.RATE_LIMITED, detail)
            if verdict == "error":
                raise HeadTurnError(HeadTurnFailure.TURN_FAILED, detail)
            if verdict == "network_halt":
                raise HeadTurnError(HeadTurnFailure.OUTCOME_UNKNOWN, detail)

            final_sid = final.get("session_id")
            if not (isinstance(final_sid, str) and final_sid):
                final_sid = sid_seen or session_id
            return HeadTurn(text, final_sid)
        finally:
            # every way out — result, failure, cancellation, any exception —
            # ends the child's group and reaps it; idempotent per child
            self._running.discard(proc)
            await self._finish_child(proc)

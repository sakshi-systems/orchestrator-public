"""Sakshi, the always-running process: it starts, claims the machine
once, serves the door and runs the reviews it accepts."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import signal
import stat
import sys
from functools import partial
from pathlib import Path

from .claude_head import ClaudeHeadRuntime
from .notify import report_review_failure
from .review_door import ReviewDoorUnavailable, ask, serve
from .review_intake import AcceptedReview, ReviewIntake
from .review_run import ReviewRun
from .review_workspace import ReviewWorkspaces
from .sakshi_settings import SETTINGS_NAME, load_sakshi_settings
from .seeds import lay_seeds
from .waiting import wait_out

LOCK_NAME = "sakshi.lock"


def _state_dir(given: str | None) -> Path:
    """The state directory resolved to its absolute path, symlinks included, whatever form
    the argument took; the default is resolved here, never at import time."""
    if given is not None:
        return Path(given).expanduser().resolve()
    return (Path.home() / ".local/state/orchestrator/sakshi").resolve()


def _prepare(directory: Path) -> None:
    """Create the state directory readable by its owner only; refuse one that is not.

    An existing directory's mode is never changed.
    """
    if directory.exists():
        mode = stat.S_IMODE(directory.stat().st_mode)
        if mode & 0o077:
            raise SystemExit(
                f"Sakshi's state directory {directory} is open to the group or to others "
                f"(mode {mode:04o}); it must be readable by its owner only"
            )
        return
    directory.mkdir(mode=0o700, parents=True)


def _claim(directory: Path) -> int:
    """Hold the non-blocking exclusive flock on sakshi.lock; the lock is the claim, not the file.

    The descriptor is non-inheritable, so a child process started later never keeps the lock
    after Sakshi has ended. The file itself stays on disk when the process ends.
    """
    lock_path = directory / LOCK_NAME
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise SystemExit(f"Sakshi cannot create its lock file {lock_path}: {exc}")
    os.set_inheritable(fd, False)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(f"Sakshi is already running: the lock {lock_path} is held")
    return fd


async def _stay(directory: Path) -> None:
    """Load the settings, bind the door, run the reviews it accepts and
    stop in order.

    The settings are loaded first, so a bad file stops the start with
    the loader's message and the lock released by _run, and the seeds
    are laid before the door is bound. The runtime,
    the workspaces and the run are built, when a repository is listed,
    before the door is bound — a missing claude stops the start naming
    it with no socket laid — and the door is bound with no repository
    listed too, its answer then being a refusal. The startup line comes
    after the handlers are installed and the socket is bound. The stop
    runs in order on every way out — a signal, a listener that ends,
    a cancellation — and a step's failure does not skip the next: the
    intake, the door, the reviews, the runtime, the door task awaited
    last, so an idle connection never holds a review's cancellation.
    A cancellation of Sakshi during the stop waits for the stop to end
    and then surfaces. A review admitted before the refusal has begun
    when it is cut, its own run recording the failure and reporting
    it. A listener that ends with an error stops Sakshi naming the
    error.
    """
    settings = load_sakshi_settings(directory / SETTINGS_NAME)
    await lay_seeds(settings)
    reviews: set[asyncio.Task] = set()
    runs: list[ReviewRun] = []

    def on_accepted(review: AcceptedReview) -> None:
        # only schedule the review: this awaits nothing and raises
        # nothing; the set holds the task until it is done
        if not runs:
            return
        task = asyncio.get_running_loop().create_task(runs[0].run(review))
        reviews.add(task)
        task.add_done_callback(reviews.discard)

    intake = ReviewIntake(settings, directory, on_accepted)
    runtime: ClaudeHeadRuntime | None = None
    if settings.repositories:
        try:
            runtime = ClaudeHeadRuntime(
                environment_names=settings.environment)
        except FileNotFoundError as error:
            raise SystemExit(str(error)) from None
        runs.append(ReviewRun(
            runtime=runtime, intake=intake,
            workspaces=ReviewWorkspaces(settings.workspace_root),
            report_failure=partial(report_review_failure, settings)))
    door = serve(directory, intake)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    door_task = asyncio.ensure_future(door.serve())
    print(f"Sakshi is running. State directory: {directory}", flush=True)
    signal_wait = asyncio.ensure_future(stopped.wait())

    async def run_every_step() -> Exception | None:
        """The stop in order — the intake, the door, the reviews, the
        runtime, the door task awaited last — every step run whatever
        an earlier one raised, the first Exception kept, to be raised
        after the last step when nothing else is on its way out."""
        error: Exception | None = None

        def keep(step: Exception) -> None:
            nonlocal error
            if error is None:
                error = step

        signal_wait.cancel()
        intake.stop()
        # a review admitted before the refusal has begun by now: yield
        # once, so its run has taken its first step and its own cancel
        # path records the failure and reports it
        await asyncio.sleep(0)
        try:
            door.aclose()
        except Exception as step:
            keep(step)
        for task in list(reviews):
            task.cancel()
        try:
            await asyncio.gather(*reviews, return_exceptions=True)
        except Exception as step:
            keep(step)
        if runtime is not None:
            try:
                await runtime.aclose()
            except Exception as step:
                keep(step)
        try:
            await asyncio.gather(door_task, return_exceptions=True)
        except Exception as step:
            keep(step)
        return error

    async def stop_waited_out() -> Exception | None:
        """The stop as a task of its own, waited out: a cancellation of
        Sakshi that lands during the stop is raised once the stop has
        ended and every step has run."""
        stop_task = asyncio.ensure_future(run_every_step())
        await wait_out(stop_task)
        return stop_task.result()

    try:
        await asyncio.wait({signal_wait, door_task},
                           return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # the stop runs on every way out, waited out; what was raised
        # surfaces once the stop has ended
        await stop_waited_out()
        raise
    step_error = await stop_waited_out()
    listener_error: SystemExit | None = None
    if not door_task.cancelled() and door_task.exception() is not None:
        # a listener that ended with an error stops Sakshi with it,
        # whether or not a signal arrived in the same turn
        listener_error = SystemExit(
            f"The review door stopped: {door_task.exception()}")
    if listener_error is not None:
        # the listener's exit surfaces; a step's error is not reported
        raise listener_error
    if step_error is not None:
        raise step_error


def _run(directory: Path) -> None:
    """Stay until a signal once the claim is held, then release the lock on every way out."""
    fd = _claim(directory)
    try:
        asyncio.run(_stay(directory))
    finally:
        os.close(fd)


def _review(args: argparse.Namespace) -> int:
    """Send one review request to the running Sakshi over its socket."""
    directory = _state_dir(args.state_dir)
    fields: dict[str, object] = {
        "repository": args.repository,
        "number": args.number,
        "head_commit": args.head_commit,
        "head_repository": args.head_repository,
    }
    try:
        answer = ask(directory, fields)
    except ReviewDoorUnavailable as e:
        print(e, file=sys.stderr)
        return 4
    if answer.accepted:
        return 0
    print(answer.refusal.value, file=sys.stderr)
    return 1


def cli() -> None:
    parser = argparse.ArgumentParser(description="Sakshi, the always-running process")
    parser.add_argument("--state-dir", help="state directory (default: ~/.local/state/orchestrator/sakshi/)")
    commands = parser.add_subparsers(dest="command")
    review = commands.add_parser(
        "review", help="send a review request to the running Sakshi")
    review.add_argument("repository", help="the repository to review, as owner/name")
    review.add_argument("number", type=int, help="the pull request's number")
    review.add_argument("--head-commit", required=True,
                        help="the head commit of the pull request")
    review.add_argument("--head-repository", required=True,
                        help="the head repository, as owner/name")
    review.add_argument("--state-dir", default=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command == "review":
        raise SystemExit(_review(args))
    directory = _state_dir(args.state_dir)
    _prepare(directory)
    _run(directory)


if __name__ == "__main__":
    cli()

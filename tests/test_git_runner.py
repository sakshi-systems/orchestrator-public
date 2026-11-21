"""Tests for orchestrator.git_runner: the real git in a hermetic clone.

The process tests ask a pid that the alias itself writes to a file and
poll ``os.kill(pid, 0)``, never pgrep: on macOS ``pgrep -a`` prints
process ids only, so a name-based ask would answer that nothing runs
whatever is running.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import stat
import time
from pathlib import Path

import pytest

import signal
import subprocess

from orchestrator import git_runner
from orchestrator.git_runner import GitError, run_git, run_git_sync

from tests.conftest import GitStand, git_in


def _gone(pid: int) -> bool:
    """True when the pid names no live process; PermissionError counts
    as gone, being what a group of only zombies answers on macOS."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return True
    return False


async def _poll(predicate, limit: float = 8.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


async def _member_pid(pid_file: Path) -> int:
    """The pid the alias wrote once its member is running.

    A file the writer has not finished — empty, without its
    terminating newline, or not all decimal digits — is read again
    after the sleep, until the deadline; a complete line returns its
    pid at once.
    """
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8")
            pid = text.strip()
            if text.endswith("\n") and pid.isdecimal():
                return int(pid)
        await asyncio.sleep(0.05)
    raise AssertionError(f"no pid was written to {pid_file}")


def _poll_sync(predicate, limit: float = 8.0) -> bool:
    """The synchronous twin of ``_poll``."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _written_pid(pid_file: Path, limit: float = 5.0) -> int:
    """The synchronous twin of ``_member_pid``: the pid a member wrote
    once it is running; the wait fails when nothing is written."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8")
            pid = text.strip()
            if text.endswith("\n") and pid.isdecimal():
                return int(pid)
        time.sleep(0.05)
    raise AssertionError(f"no pid was written to {pid_file}")


def _pid_or_none(pid_file: Path) -> int | None:
    """The pid a file holds when it holds one at all: an absent, empty
    or unfinished file answers None, so a ``finally`` that must not mask
    the failure it follows can ask without raising."""
    if not pid_file.exists():
        return None
    text = pid_file.read_text(encoding="utf-8")
    pid = text.strip()
    if text.endswith("\n") and pid.isdecimal():
        return int(pid)
    return None


def test_git_returns_its_output_with_standard_error_merged(
        git_stand: GitStand) -> None:
    async def scenario() -> tuple[str, str]:
        resolved = await run_git(
            ["rev-parse", "--verify", f"{git_stand.head_commit}^{{commit}}"],
            cwd=git_stand.checkout, timeout=30.0)
        refetched = await run_git(
            ["fetch", "origin", "refs/pull/1/head"],
            cwd=git_stand.checkout, timeout=30.0)
        return resolved, refetched

    resolved, refetched = asyncio.run(scenario())
    assert resolved.strip() == git_stand.head_commit
    assert "From" in refetched


def test_a_failing_call_raises_with_git_text_cut(git_stand: GitStand) -> None:
    async def scenario() -> tuple[GitError, GitError]:
        with pytest.raises(GitError) as raised:
            await run_git(["worktree", "add", "--detach",
                           str(git_stand.seed), git_stand.head_commit],
                          cwd=git_stand.seed, timeout=30.0)
        with pytest.raises(GitError) as cut:
            await run_git(["for-each-ref", "--sort=" + "x" * 600],
                          cwd=git_stand.seed, timeout=30.0)
        return raised.value, cut.value

    failure, cut = asyncio.run(scenario())
    assert failure.returncode != 0
    assert str(failure).startswith("git exited ")
    assert len(cut.text) == 500


def test_a_git_ended_by_a_signal_raises_with_its_negative_status(
        git_stand: GitStand) -> None:
    async def scenario() -> GitError:
        with pytest.raises(GitError) as raised:
            await run_git(["-c", "alias.suicide=!kill -9 $PPID",
                           "suicide"],
                          cwd=git_stand.checkout, timeout=30.0)
        return raised.value

    raised = asyncio.run(scenario())
    assert raised.returncode < 0


def test_git_lays_files_readable_by_others_whatever_the_umask(
        git_stand: GitStand, tmp_path: Path) -> None:
    tree = tmp_path / "tree"

    async def scenario() -> None:
        await run_git(["worktree", "add", "--detach", str(tree),
                       git_stand.head_commit],
                      cwd=git_stand.checkout, timeout=30.0)

    old = os.umask(0o077)
    try:
        asyncio.run(scenario())
    finally:
        os.umask(old)
    assert stat.S_IMODE(tree.stat().st_mode) == 0o755
    assert stat.S_IMODE((tree / "file.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE((tree / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((tree / "sub").stat().st_mode) == 0o755


def test_git_is_told_never_to_prompt(git_stand: GitStand,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> str:
        return await run_git(
            ["-c", 'alias.ask=!printf %s "$GIT_TERMINAL_PROMPT"', "ask"],
            cwd=git_stand.checkout, timeout=30.0)

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    assert asyncio.run(scenario()) == "0"
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    assert asyncio.run(scenario()) == "0"


def test_git_runs_in_the_repository_it_is_aimed_at(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    decoy = tmp_path / "decoy"
    git_in(tmp_path, "clone", str(git_stand.origin), str(decoy))
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    async def scenario() -> str:
        return await run_git(["rev-parse", "--show-toplevel"],
                             cwd=git_stand.checkout, timeout=30.0)

    toplevel = asyncio.run(scenario())
    assert Path(toplevel.strip()).resolve() == git_stand.checkout.resolve()


def test_the_child_environment_holds_no_repository_location_name(
        git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, str] = {}
    real_exec = asyncio.create_subprocess_exec

    async def recorder(program: str, *arguments: str, **kwargs):
        recorded.update(kwargs["env"])
        return await real_exec(program, *arguments, **kwargs)

    monkeypatch.setattr(git_runner.asyncio, "create_subprocess_exec",
                        recorder)
    for name in git_runner.REPOSITORY_LOCATION_VARIABLES:
        monkeypatch.setenv(name, "/a/decoy")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")

    async def scenario() -> None:
        await run_git(["rev-parse", "--git-dir"],
                      cwd=git_stand.checkout, timeout=30.0)

    asyncio.run(scenario())
    assert not (set(git_runner.REPOSITORY_LOCATION_VARIABLES)
                & set(recorded))
    assert recorded["GIT_CONFIG_COUNT"] == "0"
    assert recorded["GIT_TERMINAL_PROMPT"] == "0"


def test_a_configuration_channel_reaches_git(
        git_stand: GitStand, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.zz")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!echo carried")

    async def scenario() -> str:
        return await run_git(["zz"], cwd=git_stand.checkout, timeout=30.0)

    assert asyncio.run(scenario()).strip() == "carried"


def test_a_call_past_its_timeout_raises_and_leaves_no_member(
        git_stand: GitStand, tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    script = f"echo $$ > {shlex.quote(str(pid_file))}; sleep 300"
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> GitError:
        with pytest.raises(GitError) as raised:
            await run_git(["-c", alias, "hang"],
                          cwd=git_stand.checkout, timeout=1.0)
        pid = await _member_pid(pid_file)
        assert await _poll(lambda: _gone(pid))
        return raised.value

    raised = asyncio.run(scenario())
    assert raised.returncode == 124
    assert "timed out" in str(raised)


def test_a_member_ignoring_term_is_killed(git_stand: GitStand,
                                          tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    # A true empty trap action, spelled plainly inside the quoted script:
    # the escaped-quotes spelling reaches the inner shell as a
    # two-character action it runs and exits on, so the member would die
    # of the TERM itself.
    script = (f"echo $$ > {shlex.quote(str(pid_file))}; "
              "trap '' 15; sleep 300")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> None:
        with pytest.raises(GitError):
            await run_git(["-c", alias, "hang"],
                          cwd=git_stand.checkout, timeout=1.0)
        pid = await _member_pid(pid_file)
        assert await _poll(lambda: _gone(pid))

    asyncio.run(scenario())


def test_the_ending_reaches_a_grandchild(git_stand: GitStand,
                                         tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = (f"sleep 300 & echo $! > {shlex.quote(str(pid_file))}; "
              "wait")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> None:
        with pytest.raises(GitError):
            await run_git(["-c", alias, "hang"],
                          cwd=git_stand.checkout, timeout=1.0)
        pid = await _member_pid(pid_file)
        assert await _poll(lambda: _gone(pid))

    asyncio.run(scenario())


def test_a_git_that_exited_with_the_pipe_held_times_out_and_ends_the_member(
        git_stand: GitStand, tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    script = f"sleep 300 & echo $! > {shlex.quote(str(pid_file))}"
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> GitError:
        with pytest.raises(GitError) as raised:
            await run_git(["-c", alias, "hang"],
                          cwd=git_stand.checkout, timeout=1.0)
        pid = await _member_pid(pid_file)
        assert await _poll(lambda: _gone(pid))
        return raised.value

    raised = asyncio.run(scenario())
    assert raised.returncode == 124


def _fake_git_on_path(tmp_path: Path,
                      monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``git`` earlier on PATH that writes its pid to a file named by
    an environment variable, closes its output and keeps running: the
    parent's read sees the end of the output while git lives on."""
    pid_file = tmp_path / "member.pid"
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "git"
    script.write_text(
        "#!/bin/sh\n"
        'echo $$ > "$FAKE_GIT_PID_FILE"\n'
        "exec >/dev/null 2>&1\n"
        "sleep 300\n",
        encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_GIT_PID_FILE", str(pid_file))
    monkeypatch.setenv("PATH",
                       str(directory) + os.pathsep + os.environ["PATH"])
    return pid_file


def test_a_git_that_closes_its_output_and_keeps_running_ends_at_its_deadline(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = _fake_git_on_path(tmp_path, monkeypatch)

    async def scenario() -> tuple[GitError, float]:
        started = time.monotonic()
        with pytest.raises(GitError) as raised:
            await asyncio.wait_for(
                run_git(["status"], cwd=git_stand.checkout,
                        timeout=1.0), 12.0)
        return raised.value, time.monotonic() - started

    raised, elapsed = asyncio.run(scenario())

    async def after() -> bool:
        pid = await _member_pid(pid_file)
        return await _poll(lambda: _gone(pid))

    assert asyncio.run(after())
    assert raised.returncode == 124
    assert "timed out" in str(raised)
    assert elapsed < 10.0


def test_a_cancel_of_a_git_that_closed_its_output_leaves_no_member(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = _fake_git_on_path(tmp_path, monkeypatch)
    entered = asyncio.Event()
    real_wait = asyncio.subprocess.Process.wait

    async def waiting(self) -> int:
        entered.set()
        return await real_wait(self)

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", waiting)

    async def scenario() -> None:
        task = asyncio.create_task(
            run_git(["status"], cwd=git_stand.checkout, timeout=60.0))
        pid = await _member_pid(pid_file)
        await asyncio.wait_for(entered.wait(), 12.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 12.0)
        assert await _poll(lambda: _gone(pid))

    asyncio.run(scenario())


def test_a_cancelled_call_leaves_no_member(git_stand: GitStand,
                                           tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    script = f"echo $$ > {shlex.quote(str(pid_file))}; sleep 300"
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> None:
        task = asyncio.create_task(
            run_git(["-c", alias, "hang"],
                    cwd=git_stand.checkout, timeout=60.0))
        await _member_pid(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert await _poll(lambda: _gone(pid))

    asyncio.run(scenario())


def test_a_second_cancel_during_the_grace_still_ends_the_member(
        git_stand: GitStand, tmp_path: Path) -> None:
    """Git is stopped, so it answers no signal until it is continued:
    the ending's grace stays open for its five seconds, and a second
    cancel in that window must not leave the ending before its
    SIGKILL."""
    pid_file = tmp_path / "member.pid"
    script = (f"kill -STOP $PPID; "
              f"echo $$ > {shlex.quote(str(pid_file))}; "
              "trap '' 15; sleep 300")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> None:
        task = asyncio.create_task(
            run_git(["-c", alias, "hang"],
                    cwd=git_stand.checkout, timeout=60.0))
        await _member_pid(pid_file)
        task.cancel()
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert await _poll(lambda: _gone(pid), limit=12.0)

    asyncio.run(scenario())


def test_the_probe_treats_permission_denied_as_a_dead_group(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS a group whose only members are zombies answers EPERM to
    the probe; such a group has no live member and gets no SIGKILL."""

    class StoppedProc:
        pid = 12345

        async def wait(self) -> int:
            return 0

    signals: list = []

    def fake_killpg(pid: int, sig: int) -> None:
        signals.append(sig)
        if sig == 0:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(git_runner.os, "killpg", fake_killpg)
    asyncio.run(git_runner._end_group(StoppedProc()))
    assert signals == [signal.SIGTERM, 0]


def test_a_group_answering_permission_denied_never_stops_the_ending(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A group whose only member is a zombie answers PermissionError to
    every signal on macOS; the ending still runs to its end."""

    class ZombieProc:
        pid = 54321

        async def wait(self) -> int:
            return 0

    signals: list = []

    def fake_killpg(pid: int, sig: int) -> None:
        signals.append(sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(git_runner.os, "killpg", fake_killpg)
    asyncio.run(git_runner._end_group(ZombieProc()))
    assert signals == [signal.SIGTERM, 0]


def test_a_cancel_during_a_timeouts_ending_cancels_the_call(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Git is stopped, so the timeout's ending holds its grace open; a
    cancel in that window surfaces as a cancellation, not as the
    timeout's error, once the ending has finished."""
    monkeypatch.setattr(git_runner, "_KILL_GRACE", 1.5)
    pid_file = tmp_path / "member.pid"
    script = (f"kill -STOP $PPID; "
              f"echo $$ > {shlex.quote(str(pid_file))}; "
              "trap '' 15; sleep 300")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    async def scenario() -> None:
        task = asyncio.create_task(
            run_git(["-c", alias, "hang"],
                    cwd=git_stand.checkout, timeout=0.5))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert await _poll(lambda: _gone(pid), limit=8.0)

    asyncio.run(scenario())


def test_a_missing_directory_is_a_failed_call(tmp_path: Path) -> None:
    async def scenario() -> GitError:
        with pytest.raises(GitError) as raised:
            await run_git(["status"], cwd=tmp_path / "absent",
                          timeout=30.0)
        return raised.value

    raised = asyncio.run(scenario())
    assert raised.returncode == 127
    assert str(tmp_path / "absent") in str(raised)


def test_member_pid_waits_for_a_complete_line(tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    pid_file.write_text("", encoding="utf-8")

    async def scenario() -> int:
        async def write_later() -> None:
            await asyncio.sleep(0.2)
            pid_file.write_text("123\n", encoding="utf-8")

        writer = asyncio.create_task(write_later())
        pid = await _member_pid(pid_file)
        await writer
        return pid

    assert asyncio.run(scenario()) == 123


def test_member_pid_raises_when_nothing_is_written(
        tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"

    async def scenario() -> int:
        return await _member_pid(pid_file)

    with pytest.raises(AssertionError) as raised:
        asyncio.run(scenario())
    assert str(raised.value) == f"no pid was written to {pid_file}"


def test_run_git_sync_raises_for_a_call_past_its_bound_and_ends_the_ssh_process_it_started(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "ssh.pid"
    monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
    script = f"echo $$ > {shlex.quote(str(pid_file))}; sleep 30"
    monkeypatch.setenv("GIT_SSH_COMMAND", f"sh -c {shlex.quote(script)}")

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_git_sync(["ls-remote", "ssh://nowhere/repo.git"],
                     cwd=git_stand.checkout)
    elapsed = time.monotonic() - started

    assert elapsed < 10.0  # the module's bound, not the 120 it holds
    assert raised.value.timeout == 0.5
    pid = _written_pid(pid_file)
    assert _poll_sync(lambda: _gone(pid))


def test_a_sync_call_past_its_bound_kills_a_member_ignoring_term(
        git_stand: GitStand, tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    # A true empty trap action, spelled plainly inside the quoted script:
    # ``trap "" 15`` spelled with escaped quotes reaches the inner shell
    # as a two-character action the shell runs and then exits on, so the
    # member would die of the TERM and no escalation would be seen.
    script = (f"echo $$ > {shlex.quote(str(pid_file))}; "
              "trap '' 15; sleep 300")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    with pytest.raises(subprocess.TimeoutExpired):
        run_git_sync(["-c", alias, "hang"], cwd=git_stand.checkout,
                     timeout=1.0)

    pid = _written_pid(pid_file)
    assert _poll_sync(lambda: _gone(pid))


def test_a_sync_call_past_its_bound_ends_a_grandchild_it_started(
        git_stand: GitStand, tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = (f"sleep 300 & echo $! > {shlex.quote(str(pid_file))}; "
              "wait")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    with pytest.raises(subprocess.TimeoutExpired):
        run_git_sync(["-c", alias, "hang"], cwd=git_stand.checkout,
                     timeout=1.0)

    pid = _written_pid(pid_file)
    assert _poll_sync(lambda: _gone(pid))


def test_a_sync_call_past_its_bound_waits_for_no_process_outside_the_group(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The ending reaps git and closes its captured pipes without
    reading them: a hook's helper that left the group under its own
    session and holds the inherited pipe cannot keep the call open past
    its bound and the grace — the raise comes at the bound, not at the
    escapee's lifetime."""
    member = tmp_path / "member.pid"
    outside = tmp_path / "outside.pid"
    escapee = tmp_path / "escapee.py"
    escapee.write_text(
        "import os, sys, time\n"
        "os.setsid()\n"
        "with open(sys.argv[1], 'w') as handle:\n"
        "    handle.write(str(os.getpid()) + '\\n')\n"
        "time.sleep(20)\n",
        encoding="utf-8")
    script = (f"echo $$ > {shlex.quote(str(member))}; "
              f"python3 {shlex.quote(str(escapee))} "
              f"{shlex.quote(str(outside))} & "
              "trap '' 15; sleep 300")
    alias = f"alias.hang=!sh -c {shlex.quote(script)}"

    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_git_sync(["-c", alias, "hang"], cwd=git_stand.checkout,
                         timeout=1.0)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0 + git_runner._KILL_GRACE + 2.0
        assert _poll_sync(lambda: _gone(_written_pid(member)))
    finally:
        # The escapee left git's group and outlives the call by design;
        # end it here so the test leaves nothing behind — a failing
        # assertion above included, its failure never masked: no pid, no
        # kill, and a signal that lands too late is tolerated.
        pid = _pid_or_none(outside)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_a_sync_call_takes_its_bound_from_the_call_not_the_module_constant(
        git_stand: GitStand, tmp_path: Path) -> None:
    """The module's constant stays at its 120; a member that ends after
    the call's own bound still raises, so the number the call carries is
    the bound it runs under."""
    alias = "alias.slow=!sh -c 'sleep 1.2; echo done'"

    with pytest.raises(subprocess.TimeoutExpired):
        run_git_sync(["-c", alias, "slow"], cwd=git_stand.checkout,
                     timeout=1.0)


def test_a_sync_call_with_no_bound_returns_git_s_result(
        git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """``timeout=None`` waits with no bound: a member that outlives the
    module's lowered constant still runs to its end and the call
    returns git's result."""
    monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
    alias = "alias.slow=!sh -c 'sleep 1.2; echo done'"

    completed = run_git_sync(["-c", alias, "slow"], cwd=git_stand.checkout,
                             timeout=None)

    assert completed.returncode == 0
    assert completed.stdout.strip() == "done"


def test_run_git_sync_returns_a_failed_process_without_raising(
        git_stand: GitStand) -> None:
    completed = run_git_sync(["rev-parse", "--verify", "refs/heads/absent"],
                             cwd=git_stand.checkout)

    assert completed.returncode != 0
    assert isinstance(completed.stderr, str)
    assert completed.stderr  # a call that writes to standard error on every machine


def test_a_standard_output_byte_the_locale_cannot_decode_round_trips(
        git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The standard output is read as paths and ref names that travel
    back to git as arguments: a byte the locale cannot decode comes home
    as a lone surrogate, and ``os.fsencode`` of it is the byte again."""
    monkeypatch.setattr(git_runner.locale, "getpreferredencoding",
                        lambda *args, **kwargs: "ascii")
    alias = r"alias.raw=!printf '\351'"

    completed = run_git_sync(["-c", alias, "raw"], cwd=git_stand.checkout)

    assert completed.returncode == 0
    assert os.fsencode(completed.stdout) == b"\xe9"


def test_a_standard_error_byte_the_locale_cannot_decode_stays_printable(
        git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a person reads the standard error: it never raises at decode
    and never at print, the byte stays visible as ``\\xe9``, and the
    status git exited with is still the status the call returns."""
    monkeypatch.setattr(git_runner.locale, "getpreferredencoding",
                        lambda *args, **kwargs: "ascii")
    alias = r"alias.raw=!printf '\351' 1>&2; exit 3"

    completed = run_git_sync(["-c", alias, "raw"], cwd=git_stand.checkout)

    assert completed.returncode == 3
    assert "\\xe9" in completed.stderr


def test_run_git_sync_reads_the_repository_in_cwd_not_an_ambient_one(
        git_stand: GitStand, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    git_in(tmp_path, "clone", str(git_stand.origin), str(other))
    (other / "file.txt").write_text("another repository's work\n", encoding="utf-8")
    git_in(other, "add", "-A")
    git_in(other, "commit", "-q", "-m", "the other repository's commit")

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    resolved = run_git_sync(["rev-parse", "HEAD"], cwd=git_stand.checkout)

    assert resolved.returncode == 0
    assert resolved.stdout.strip() == git_stand.main_commit


def test_run_git_sync_treats_a_named_pathspec_as_a_literal_path(
        git_stand: GitStand) -> None:
    (git_stand.checkout / "plain.txt").write_text("plain\n", encoding="utf-8")
    (git_stand.checkout / "*.txt").write_text("the literal one\n", encoding="utf-8")
    run_git_sync(["add", "--", "plain.txt", "*.txt"], cwd=git_stand.checkout)

    listed = run_git_sync(["ls-files", "--", "*.txt"], cwd=git_stand.checkout)

    assert listed.returncode == 0
    assert listed.stdout.splitlines() == ["*.txt"]

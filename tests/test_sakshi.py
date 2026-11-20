"""Sakshi starts, stays running as a singleton, serves the door, runs
the reviews it accepts and stops.

Every test that starts Sakshi uses a short directory under /tmp: on
macOS a socket path of 104 bytes or more does not bind, and /tmp
resolves to /private/tmp, so pytest's tmp_path is too long there.
"""

import asyncio
import json
import os
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from orchestrator import sakshi as sakshi_module
import orchestrator.review_door as review_door
from orchestrator.review_door import ask
from orchestrator.review_intake import ReviewIntake
from orchestrator.reviews import ReviewRequest
from orchestrator.sakshi import _claim, _run, _stay

from tests.test_review_run import FakeRuntime

REPO = Path(__file__).resolve().parent.parent
STARTUP_PREFIX = "Sakshi is running. State directory: "
TIMEOUT = 10.0

_SHORT_ROOTS: list[Path] = []


def _short(prefix: str) -> Path:
    """A fresh short directory under /tmp, resolved, removed when the
    test ends: Sakshi prints its state directory resolved, and /tmp
    itself resolves to /private/tmp on macOS."""
    directory = Path(tempfile.mkdtemp(
        prefix=f"sx{prefix}-", dir="/tmp")).resolve()
    _SHORT_ROOTS.append(directory)
    return directory


@pytest.fixture(autouse=True)
def _clean_short_roots():
    yield
    while _SHORT_ROOTS:
        shutil.rmtree(_SHORT_ROOTS.pop(), ignore_errors=True)


def _spawn(args: list[str], *, cwd: Path = REPO, env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "orchestrator.sakshi", *args],
        cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _try_start(state_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.sakshi", "--state-dir", str(state_dir)],
        cwd=str(REPO), capture_output=True, timeout=TIMEOUT,
    )


@pytest.fixture
def sakshi():
    """Spawn Sakshi subprocesses and stop each of them when the test ends, however it ends."""
    started = []

    def spawn(args: list[str], **kwargs) -> subprocess.Popen:
        process = _spawn(args, **kwargs)
        started.append(process)
        return process

    yield spawn

    for process in started:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _wait_for_startup(process: subprocess.Popen, state_dir: Path) -> str:
    """Poll stdout for the startup line naming the state directory; never sleep a fixed time."""
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + TIMEOUT
    seen = []
    try:
        while time.monotonic() < deadline:
            if not selector.select(0.1):
                continue
            line = process.stdout.readline()
            if not line:
                break
            seen.append(line.decode())
            if seen[-1] == f"{STARTUP_PREFIX}{state_dir}\n":
                return seen[-1]
        stderr = process.stderr.read().decode() if process.poll() is not None else "(still running)"
        raise AssertionError(f"no startup line within {TIMEOUT}s: saw {seen!r}, stderr {stderr}")
    finally:
        selector.close()


def _wait_exit(process: subprocess.Popen) -> int:
    try:
        process.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"{process.args} did not exit within {TIMEOUT}s")
    return process.returncode


def test_missing_state_directory_is_created_owner_only_and_named(sakshi):
    state = _short("state")
    process = sakshi(["--state-dir", str(state)])

    line = _wait_for_startup(process, state)

    # the waited-for name is the resolved one, whatever the host
    # resolves /tmp to (on macOS, /private/tmp)
    assert state == state.resolve()
    assert line == f"{STARTUP_PREFIX}{state}\n"
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700


def test_second_start_on_the_same_directory_is_refused(sakshi):
    state = _short("state")
    first = sakshi(["--state-dir", str(state)])
    _wait_for_startup(first, state)

    second = _try_start(state)

    assert second.returncode != 0
    assert str(state / "sakshi.lock") in second.stderr.decode()
    assert first.poll() is None

    third = _try_start(state)
    assert third.returncode != 0
    assert str(state / "sakshi.lock") in third.stderr.decode()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_ends_the_process_with_status_0(sakshi, sig):
    state = _short("state")
    process = sakshi(["--state-dir", str(state)])
    _wait_for_startup(process, state)

    process.send_signal(sig)

    assert _wait_exit(process) == 0


def test_after_sigkill_a_new_start_takes_the_lock(sakshi):
    state = _short("state")
    first = sakshi(["--state-dir", str(state)])
    _wait_for_startup(first, state)
    lock = state / "sakshi.lock"
    assert lock.exists()

    first.kill()
    _wait_exit(first)

    assert lock.exists()
    second = sakshi(["--state-dir", str(state)])
    assert _wait_for_startup(second, state) == f"{STARTUP_PREFIX}{state}\n"


def test_a_group_open_state_directory_is_refused_unchanged():
    state = _short("state")
    os.chmod(state, 0o755)

    result = _try_start(state)

    assert result.returncode != 0
    message = result.stderr.decode()
    assert str(state) in message
    assert "0755" in message
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o755


def test_default_state_directory_under_home_is_created_and_named(sakshi):
    """Without --state-dir the default lives under HOME, parents included, and is what the
    startup line names."""
    home = _short("home")
    process = sakshi([], env={**os.environ, "HOME": str(home)})

    default = (home / ".local/state/orchestrator/sakshi").resolve()
    line = _wait_for_startup(process, default)

    assert line == f"{STARTUP_PREFIX}{default}\n"
    assert stat.S_IMODE(os.stat(default).st_mode) == 0o700


def test_relative_state_dir_is_named_as_the_absolute_path(sakshi):
    """A relative --state-dir is resolved against the working directory before it is named."""
    work = _short("work")
    process = sakshi(["--state-dir", "rel-state"], cwd=work)

    resolved = (work / "rel-state").resolve()
    line = _wait_for_startup(process, resolved)

    assert line == f"{STARTUP_PREFIX}{resolved}\n"


def test_the_claims_descriptor_is_not_inheritable(tmp_path):
    """A child started later must never keep the lock after Sakshi has ended."""
    state = tmp_path / "sakshi-state"
    state.mkdir(mode=0o700)

    fd = _claim(state)
    try:
        assert os.get_inheritable(fd) is False
    finally:
        os.close(fd)


def _review_command(state: Path, commit: str) -> list[str]:
    return [sys.executable, "-m", "orchestrator.sakshi", "review",
            "owner/name", "1", "--head-commit", commit,
            "--head-repository", "owner/name",
            "--state-dir", str(state)]


def test_a_running_sakshi_without_settings_refuses_the_repository(
        sakshi) -> None:
    state = _short("state")
    process = sakshi(["--state-dir", str(state)])
    _wait_for_startup(process, state)

    result = subprocess.run(_review_command(state, "a" * 40),
                            cwd=str(REPO), capture_output=True,
                            timeout=TIMEOUT)

    assert result.returncode == 1
    assert result.stderr.decode().strip() == "repository-not-reviewed"


def test_an_invalid_settings_file_stops_the_start() -> None:
    state = _short("state")
    (state / "sakshi.json").write_text("{", encoding="utf-8")

    result = _try_start(state)

    assert result.returncode != 0
    assert str(state / "sakshi.json") in result.stderr.decode()


def _settings_payload(git_stand, workspace_root: Path,
                      memory: Path) -> dict:
    return {
        "workspace_root": str(workspace_root),
        "environment": [],
        "repositories": {
            "owner/name": {
                "checkout": str(git_stand.checkout),
                "team": {
                    "memory": str(memory),
                    "deadline_seconds": 30,
                    "roles": {
                        "reviewer-1": {
                            "seed": str(memory.parent / "seeds"
                                        / "reviewer-1"),
                            "user": "review_01",
                            "model": "claude-3",
                            "effort": "high",
                            "head": "01",
                        }
                    },
                    "musafir": {
                        "seed": str(memory.parent / "seeds" / "musafir"),
                        "user": "review_04",
                        "model": "claude-3",
                        "effort": "medium",
                        "head": "04",
                    },
                },
            }
        },
    }


def _record_state(state: Path, git_stand) -> str | None:
    record = (state / "reviews" / "owner" / "name" / "1"
              / f"{git_stand.head_commit}.json")
    if not record.exists():
        return None
    return json.loads(record.read_text(encoding="utf-8"))["state"]


def _ready(state: Path) -> bool:
    return (state / "review.sock").exists()


def test_a_request_reaches_the_roles_the_musafir_and_finishes(
        tmp_path, git_stand, monkeypatch) -> None:
    state = _short("state")
    workspace_root = _short("ws")
    home = _short("home")
    memory = home / "memory"
    memory.mkdir()
    seeds = home / "seeds"
    (seeds / "reviewer-1").mkdir(parents=True)
    (seeds / "musafir").mkdir(parents=True)
    (state / "sakshi.json").write_text(json.dumps(_settings_payload(
        git_stand, workspace_root, memory)), encoding="utf-8")
    runtime = FakeRuntime({})
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario() -> None:
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + 10.0
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert _ready(state)
        answer = await asyncio.to_thread(
            ask, state,
            {"repository": "owner/name", "number": 1,
             "head_commit": git_stand.head_commit,
             "head_repository": "owner/name"})
        assert answer.accepted
        deadline = time.monotonic() + 15.0
        while (_record_state(state, git_stand) != "finished"
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await staying

    asyncio.run(scenario())
    assert [start["cwd"].name for start in runtime.starts] == [
        "reviewer-1", "musafir"]
    assert _record_state(state, git_stand) == "finished"
    assert runtime.active == 0


def test_a_stop_during_the_roles_ends_them_and_records_failed(
        tmp_path, git_stand, monkeypatch) -> None:
    state = _short("state")
    workspace_root = _short("ws")
    home = _short("home")
    memory = home / "memory"
    memory.mkdir()
    seeds = home / "seeds"
    (seeds / "reviewer-1").mkdir(parents=True)
    (seeds / "musafir").mkdir(parents=True)
    (state / "sakshi.json").write_text(json.dumps(_settings_payload(
        git_stand, workspace_root, memory)), encoding="utf-8")

    async def hold(add_dirs) -> str:
        await asyncio.sleep(30)
        return "late"

    runtime = FakeRuntime({"reviewer-1": hold})
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario() -> None:
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + 10.0
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        answer = await asyncio.to_thread(
            ask, state,
            {"repository": "owner/name", "number": 1,
             "head_commit": git_stand.head_commit,
             "head_repository": "owner/name"})
        assert answer.accepted
        deadline = time.monotonic() + 10.0
        while ("reviewer-1" not in runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
        assert "reviewer-1" in runtime.entered
        os.kill(os.getpid(), signal.SIGTERM)
        await staying

    asyncio.run(scenario())
    assert runtime.cancelled == ["reviewer-1"]
    assert runtime.active == 0
    assert _record_state(state, git_stand) == "failed"


def test_a_missing_claude_stops_the_start_with_no_socket_laid(
        git_stand, monkeypatch) -> None:
    state = _short("state")
    workspace_root = _short("ws")
    home = _short("home")
    memory = home / "memory"
    (state / "sakshi.json").write_text(json.dumps(_settings_payload(
        git_stand, workspace_root, memory)), encoding="utf-8")

    def missing(**kwargs):
        raise FileNotFoundError(
            "claude CLI not found. Install it with: "
            "npm install -g @anthropic-ai/claude-code")

    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime", missing)
    with pytest.raises(SystemExit) as raised:
        _run(state)
    assert "claude" in str(raised.value)
    # no socket was laid, and the lock is free to claim again
    assert not (state / "review.sock").exists()
    os.close(_claim(state))


def test_the_stop_runs_in_order(tmp_path, git_stand, monkeypatch) -> None:
    state = _short("state")
    workspace_root = _short("ws")
    home = _short("home")
    memory = home / "memory"
    memory.mkdir()
    seeds = home / "seeds"
    (seeds / "reviewer-1").mkdir(parents=True)
    (seeds / "musafir").mkdir(parents=True)
    (state / "sakshi.json").write_text(json.dumps(_settings_payload(
        git_stand, workspace_root, memory)), encoding="utf-8")
    order: list[str] = []

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    real_serve = sakshi_module.serve

    def recording_serve(directory, intake):
        door = real_serve(directory, intake)
        real_aclose = door.aclose

        def recording_aclose() -> None:
            order.append("door")
            real_aclose()

        door.aclose = recording_aclose
        return door

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    async def hold(add_dirs) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            order.append("cancelled")
            raise

    runtime = RecordingRuntime({"reviewer-1": hold})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", recording_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario() -> None:
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + 10.0
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        answer = await asyncio.to_thread(
            ask, state,
            {"repository": "owner/name", "number": 1,
             "head_commit": git_stand.head_commit,
             "head_repository": "owner/name"})
        assert answer.accepted
        deadline = time.monotonic() + 10.0
        while ("reviewer-1" not in runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await staying

    asyncio.run(scenario())
    assert order == ["intake", "door", "cancelled", "runtime"]
    assert runtime.cancelled == ["reviewer-1"]
    assert _record_state(state, git_stand) == "failed"


def _stop_fixture(git_stand) -> Path:
    """The state a stopping test needs: a short state directory, a
    settings file over the stand, and the seed directories."""
    state = _short("state")
    workspace_root = _short("ws")
    home = _short("home")
    memory = home / "memory"
    memory.mkdir()
    seeds = home / "seeds"
    (seeds / "reviewer-1").mkdir(parents=True)
    (seeds / "musafir").mkdir(parents=True)
    (state / "sakshi.json").write_text(json.dumps(_settings_payload(
        git_stand, workspace_root, memory)), encoding="utf-8")
    return state


def test_a_door_that_fails_stops_sakshi_and_runs_the_stop(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    socket_path = state / "review.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    class BoomDoor:
        async def serve(self) -> None:
            raise OSError("boom")

        def aclose(self) -> None:
            order.append("door")
            listener.close()
            socket_path.unlink()

    def boom_serve(directory, intake):
        listener.bind(str(socket_path))
        return BoomDoor()

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    runtime = RecordingRuntime({})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", boom_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)
    staying: dict = {}

    async def scenario():
        staying["task"] = asyncio.ensure_future(_stay(state))
        done, pending = await asyncio.wait({staying["task"]},
                                           timeout=TIMEOUT)
        assert not pending

    try:
        asyncio.run(scenario())
    except SystemExit as ended:
        error = ended
    else:
        raise AssertionError("_stay ended without a SystemExit")
    staying["task"].exception()
    assert str(error) == "The review door stopped: boom"
    assert order == ["intake", "door", "runtime"]
    assert not socket_path.exists()


def test_a_door_that_fails_cancels_a_held_review_before_the_runtime(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    socket_path = state / "review.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held: dict = {}

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    class BoomAfterRoleDoor:
        async def serve(self) -> None:
            while "reviewer-1" not in runtime.entered:
                await asyncio.sleep(0.05)
            raise OSError("boom")

        def aclose(self) -> None:
            order.append("door")
            listener.close()
            socket_path.unlink()

    def boom_serve(directory, intake):
        held["intake"] = intake
        listener.bind(str(socket_path))
        return BoomAfterRoleDoor()

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    async def hold(add_dirs) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            order.append("cancelled")
            raise

    runtime = RecordingRuntime({"reviewer-1": hold})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", boom_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)
    staying: dict = {}

    async def scenario():
        staying["task"] = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + TIMEOUT
        while "intake" not in held and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        answer = held["intake"].admit(ReviewRequest(
            repository="owner/name", number=1,
            head_commit=git_stand.head_commit,
            head_repository="owner/name"))
        assert answer.accepted
        done, pending = await asyncio.wait({staying["task"]},
                                           timeout=TIMEOUT)
        assert not pending

    try:
        asyncio.run(scenario())
    except SystemExit as ended:
        error = ended
    else:
        raise AssertionError("_stay ended without a SystemExit")
    staying["task"].exception()
    assert str(error) == "The review door stopped: boom"
    assert order == ["intake", "door", "cancelled", "runtime"]
    assert runtime.cancelled == ["reviewer-1"]


def test_a_cancelled_stay_still_closes_the_runtime(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    socket_path = state / "review.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    door_started: list[bool] = []
    serving = asyncio.Event()

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    class QuietDoor:
        async def serve(self) -> None:
            door_started.append(True)
            await serving.wait()

        def aclose(self) -> None:
            order.append("door")
            listener.close()
            socket_path.unlink()
            serving.set()

    def quiet_serve(directory, intake):
        listener.bind(str(socket_path))
        return QuietDoor()

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    runtime = RecordingRuntime({})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", quiet_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario():
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + TIMEOUT
        while not door_started and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        staying.cancel()
        try:
            await staying
        except asyncio.CancelledError:
            return "cancelled"
        return "ended"

    assert asyncio.run(scenario()) == "cancelled"
    assert order == ["intake", "door", "runtime"]
    assert not socket_path.exists()


def test_a_failing_door_close_still_cancels_the_reviews_and_closes_the_runtime(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    real_serve = sakshi_module.serve

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    def failing_serve(directory, intake):
        door = real_serve(directory, intake)
        real_aclose = door.aclose

        def failing_aclose() -> None:
            order.append("door")
            real_aclose()
            raise PermissionError(1, "the state directory is read-only")

        door.aclose = failing_aclose
        return door

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    async def hold(add_dirs) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            order.append("cancelled")
            raise

    runtime = RecordingRuntime({"reviewer-1": hold})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", failing_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario():
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + TIMEOUT
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        answer = await asyncio.to_thread(
            ask, state,
            {"repository": "owner/name", "number": 1,
             "head_commit": git_stand.head_commit,
             "head_repository": "owner/name"})
        assert answer.accepted
        deadline = time.monotonic() + TIMEOUT
        while ("reviewer-1" not in runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await staying
        except PermissionError as ended:
            return ended
        return None

    ended = asyncio.run(scenario())
    assert isinstance(ended, PermissionError)
    assert order == ["intake", "door", "cancelled", "runtime"]
    assert runtime.cancelled == ["reviewer-1"]


def test_a_failing_door_close_does_not_hide_a_stopped_listener(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    class BoomDoor:
        async def serve(self) -> None:
            raise OSError("boom")

        def aclose(self) -> None:
            order.append("door")
            raise PermissionError(1, "the state directory is read-only")

    def boom_serve(directory, intake):
        return BoomDoor()

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    runtime = RecordingRuntime({})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", boom_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)
    staying: dict = {}

    async def scenario():
        staying["task"] = asyncio.ensure_future(_stay(state))
        done, pending = await asyncio.wait({staying["task"]},
                                           timeout=TIMEOUT)
        assert not pending

    try:
        asyncio.run(scenario())
    except SystemExit as ended:
        error = ended
    else:
        raise AssertionError("_stay ended without a SystemExit")
    staying["task"].exception()
    assert str(error) == "The review door stopped: boom"
    assert order == ["intake", "door", "runtime"]


def test_an_idle_client_does_not_hold_a_reviews_cancellation(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    monkeypatch.setattr(review_door, "_READ_SECONDS", 30.0)
    real_serve = sakshi_module.serve

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    def recording_serve(directory, intake):
        door = real_serve(directory, intake)
        real_aclose = door.aclose

        def recording_aclose() -> None:
            order.append("door")
            real_aclose()

        door.aclose = recording_aclose
        return door

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    async def hold(add_dirs) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            order.append("cancelled")
            raise

    runtime = RecordingRuntime({"reviewer-1": hold})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", recording_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario():
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + TIMEOUT
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        answer = await asyncio.to_thread(
            ask, state,
            {"repository": "owner/name", "number": 1,
             "head_commit": git_stand.head_commit,
             "head_repository": "owner/name"})
        assert answer.accepted
        deadline = time.monotonic() + TIMEOUT
        while ("reviewer-1" not in runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
        idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        idle.connect(str(state / "review.sock"))
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            deadline = time.monotonic() + TIMEOUT
            while ("cancelled" not in order
                   and time.monotonic() < deadline):
                await asyncio.sleep(0.05)
            assert "cancelled" in order
        finally:
            idle.close()
        done, pending = await asyncio.wait({staying}, timeout=TIMEOUT)
        assert not pending
        await staying

    asyncio.run(scenario())
    assert order == ["intake", "door", "cancelled", "runtime"]
    assert runtime.cancelled == ["reviewer-1"]


def test_a_cancellation_during_the_stop_waits_for_the_stop_to_end(
        tmp_path, git_stand, monkeypatch) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    real_serve = sakshi_module.serve
    staying_box: dict = {}

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    def recording_serve(directory, intake):
        door = real_serve(directory, intake)
        real_aclose = door.aclose

        def recording_aclose() -> None:
            order.append("door")
            real_aclose()

        door.aclose = recording_aclose
        return door

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    async def hold(add_dirs) -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # the embedder's cancel lands while the stop waits here
            staying_box["stay"].cancel()
            await asyncio.sleep(1)
            order.append("cancelled")
            raise

    runtime = RecordingRuntime({"reviewer-1": hold})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", recording_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario():
        staying = asyncio.ensure_future(_stay(state))
        staying_box["stay"] = staying
        deadline = time.monotonic() + TIMEOUT
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        answer = await asyncio.to_thread(
            ask, state,
            {"repository": "owner/name", "number": 1,
             "head_commit": git_stand.head_commit,
             "head_repository": "owner/name"})
        assert answer.accepted
        deadline = time.monotonic() + TIMEOUT
        while ("reviewer-1" not in runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await staying
        except asyncio.CancelledError:
            return "cancelled"
        return "ended"

    assert asyncio.run(scenario()) == "cancelled"
    assert order == ["intake", "door", "cancelled", "runtime"]
    assert runtime.cancelled == ["reviewer-1"]
    assert not (state / "review.sock").exists()
    assert _record_state(state, git_stand) == "failed"


def test_a_review_admitted_inside_the_stop_is_cut_by_it(
        tmp_path, git_stand, monkeypatch, capsys) -> None:
    order: list[str] = []
    state = _stop_fixture(git_stand)
    real_serve = sakshi_module.serve

    class AdmittingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            answer = self.admit(ReviewRequest(
                repository="owner/name", number=1,
                head_commit=git_stand.head_commit,
                head_repository="owner/name"))
            assert answer.accepted
            super().stop()

    def recording_serve(directory, intake):
        door = real_serve(directory, intake)
        real_aclose = door.aclose

        def recording_aclose() -> None:
            order.append("door")
            real_aclose()

        door.aclose = recording_aclose
        return door

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    runtime = RecordingRuntime({})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", AdmittingIntake)
    monkeypatch.setattr(sakshi_module, "serve", recording_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)

    async def scenario():
        staying = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + TIMEOUT
        while not _ready(state) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await staying

    asyncio.run(scenario())
    assert _record_state(state, git_stand) == "failed"
    assert runtime.entered == []
    assert order == ["intake", "door", "runtime"]
    assert capsys.readouterr().out.count(
        "Review owner/name#1 failed: stop-cut-the-review") == 1


@pytest.mark.parametrize("signal_first", [False, True],
                         ids=["event-first", "signal-first"])
def test_a_listeners_error_survives_a_stop_in_the_same_turn(
        tmp_path, git_stand, monkeypatch, signal_first) -> None:
    order: list[str] = []
    turn: list[str] = []
    state = _stop_fixture(git_stand)
    callbacks: dict = {}

    class RecordingIntake(ReviewIntake):
        def stop(self) -> None:
            order.append("intake")
            super().stop()

    class EventfulBoomDoor:
        def __init__(self) -> None:
            self.boom = asyncio.Event()

        async def serve(self) -> None:
            await self.boom.wait()
            turn.append("listener")
            raise OSError("boom")

        def aclose(self) -> None:
            order.append("door")
            self.boom.set()

    door_box: dict = {}

    def boom_serve(directory, intake):
        door_box["door"] = EventfulBoomDoor()
        return door_box["door"]

    class RecordingRuntime(FakeRuntime):
        async def aclose(self) -> None:
            order.append("runtime")

    runtime = RecordingRuntime({})
    monkeypatch.setattr(sakshi_module, "ReviewIntake", RecordingIntake)
    monkeypatch.setattr(sakshi_module, "serve", boom_serve)
    monkeypatch.setattr(sakshi_module, "ClaudeHeadRuntime",
                        lambda **kwargs: runtime)
    staying: dict = {}

    async def scenario():
        loop = asyncio.get_running_loop()

        def capture(sig, callback, *args):
            # the handler _stay registers, run by the test's choice of
            # order and not by the signal path's timing
            callbacks[sig] = callback

        loop.add_signal_handler = capture
        staying["task"] = asyncio.ensure_future(_stay(state))
        deadline = time.monotonic() + TIMEOUT
        while "door" not in door_box and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        def stop() -> None:
            callbacks[signal.SIGTERM]()
            turn.append("stop")

        if signal_first:
            loop.call_soon(stop)
            door_box["door"].boom.set()
        else:
            door_box["door"].boom.set()
            loop.call_soon(stop)
        done, pending = await asyncio.wait({staying["task"]},
                                           timeout=TIMEOUT)
        assert not pending

    try:
        asyncio.run(scenario())
    except SystemExit as ended:
        error = ended
    else:
        raise AssertionError("_stay ended without a SystemExit")
    staying["task"].exception()
    assert turn == (["stop", "listener"] if signal_first
                    else ["listener", "stop"])
    assert str(error) == "The review door stopped: boom"
    assert order == ["intake", "door", "runtime"]

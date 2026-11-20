"""A Claude head's turn is one `claude -p --resume` call, played against a
fake executable that records how it was called.

The real claude is never invoked and the real ~/.claude is never read:
CLAUDE_CONFIG_DIR points at a temporary directory holding the session's
transcript.
"""

import asyncio
import json
import os
import signal
import subprocess
import time
import tracemalloc
from pathlib import Path

import pytest

from orchestrator import claude_head
from orchestrator.claude_cli import TURN_DISALLOWED_TOOLS
from orchestrator.claude_head import ClaudeHeadRuntime
from orchestrator.heads import HeadTurn, HeadTurnError, HeadTurnFailure

SESSION_ID = "3f2b8a10-9c4d-4e5f-8a7b-1c2d3e4f5a6b"
NO_TRANSCRIPT_ID = "11111111-2222-4333-8444-555555555555"
OTHER_DIR_ID = "7a1c9e20-5d3b-4f8a-b2c6-9e0d1a2b3c4f"
PROJECT_KEY = "-home-head-work"

FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Append one JSON line per event to the record file. Modes, by
FAKE_CLAUDE_AFTER: "" plays the stream and exits; "sleep" plays it and
stays 5s; "serve" starts a grandchild and waits; "stubborn" ignores SIGTERM
and waits; "term-grand" starts a grandchild that ignores SIGTERM and waits;
"linger" plays the stream and then waits holding stdout open; "flood"
writes 40 MB of plain 100-byte lines and exits 0 with no event;
"holdwait" plays the stream, closes stdout and stderr and then closes
descriptors 1 and 2 themselves — closing a standard stream leaves its
descriptor open, so the caller sees the end of the stream only once the
descriptors are closed, while this fake still runs — then notes
"waiting" and waits with a grandchild detached from the stream."""
import json
import os
import signal
import subprocess
import sys
import time

# under a stripped environment a python child cannot resolve its own
# executable, so the grandchild runs by an absolute path
INTERPRETER = sys.executable or "/usr/bin/python3"

mode = os.environ.get("FAKE_CLAUDE_AFTER", "")
record = {
    "phase": "start",
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "stdin": "eof-at-once" if sys.stdin.buffer.read(1) == b"" else "inherited",
    "disable_background_tasks": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
    "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    "environment": dict(os.environ),
}
if mode in ("serve", "term-grand", "holdwait", "stubborn"):
    if mode == "term-grand":
        # the grandchild must have SIGTERM ignored before this fake is
        # considered ready, so a SIGTERM to the group cannot win the race
        # with its handler installation
        args = [INTERPRETER, "-c", "import os, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "open(os.environ['FAKE_CLAUDE_GC_READY'], 'w').write('r'); "
                "time.sleep(300)"]
    else:
        args = [INTERPRETER, "-c", "import time; time.sleep(300)"]
    if mode == "holdwait":
        # detached from the stream, so the caller sees its end while this
        # fake still runs
        spawned = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        spawned = subprocess.Popen(args)
    record["grandchild_pid"] = spawned.pid
    if mode == "term-grand":
        while (not os.path.exists(os.environ["FAKE_CLAUDE_GC_READY"])
               and spawned.poll() is None):
            time.sleep(0.01)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["FAKE_CLAUDE_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
if mode == "plain":
    # one plain-text line on standard error and exit 1: what the CLI
    # answers a session id already in use, writing no event at all
    sys.stderr.write(os.environ.get(
        "FAKE_CLAUDE_PLAIN", "Error: Session ID is already in use.") + "\\n")
    sys.stderr.flush()
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "1")))
if mode in ("serve", "stubborn", "term-grand"):
    time.sleep(300)
    sys.exit(0)
if mode == "flood":
    # 40 MB of plain 100-byte lines, no event: a reader that kept every
    # line would hold about a hundred megabytes of them
    block = (b"p" * 99 + b"\\n") * 400
    for _ in range(1000):
        sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    sys.exit(0)
with open(os.environ["FAKE_CLAUDE_STREAM"], "rb") as handle:
    sys.stdout.buffer.write(handle.read())
sys.stdout.buffer.flush()
if mode == "linger":
    time.sleep(300)
    sys.exit(0)
if mode == "holdwait":
    sys.stdout.close()
    sys.stderr.close()
    # a standard stream's close() leaves its descriptor open, so the
    # descriptors themselves are closed: the caller sees the end of the
    # stream while this fake still runs
    os.close(1)
    os.close(2)
    with open(os.environ["FAKE_CLAUDE_RECORD"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": "waiting", "pid": os.getpid()}) + "\\n")
    time.sleep(300)
    sys.exit(0)
if mode == "sleep":
    time.sleep(5)
with open(os.environ["FAKE_CLAUDE_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"phase": "end", "pid": os.getpid()}) + "\\n")
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''


def _event(**fields) -> bytes:
    return json.dumps(fields).encode("utf-8")


FAKE_SUDO = '''#!/usr/bin/env python3
"""A fake sudo: it checks the calling shape -n -u <user> -H --, records
its argv, and runs the rest. FAKE_SUDO_MODE picks how: "" execs the
rest, as sudo runs its command; "refuse" prints one plain line and
exits 1; "dummy" answers every kill with exit 0 without killing
anything, ignores SIGTERM itself and runs the rest as a child."""
import json
import os
import signal
import sys

mode = os.environ.get("FAKE_SUDO_MODE", "")
argv = sys.argv[1:]
if (len(argv) < 6 or argv[0] != "-n" or argv[1] != "-u"
        or argv[3] != "-H" or argv[4] != "--"):
    print("fake sudo: usage is -n -u <user> -H -- <command>",
          file=sys.stderr)
    sys.exit(2)
user = argv[2]
command = argv[5:]
record_path = os.environ.get("FAKE_SUDO_RECORD")
if record_path:
    with open(record_path, "a", encoding="utf-8") as handle:
        print(json.dumps({"user": user, "argv": argv,
                          "pid": os.getpid()}), file=handle)
if mode == "refuse":
    print("sudo: a password is required", file=sys.stderr)
    sys.exit(1)
if os.path.basename(command[0]) == "kill":
    if mode == "dummy":
        sys.exit(0)
    os.execv(command[0], command)
if mode == "dummy":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = os.fork()
    if child == 0:
        os.execv(command[0], command)
    os.waitpid(child, 0)
    sys.exit(0)
os.execv(command[0], command)
'''


def _lines(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in
            record.read_text(encoding="utf-8").splitlines() if line.strip()]


def _starts(record: Path) -> list[dict]:
    return [line for line in _lines(record) if line.get("phase") == "start"]


def _poll(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


async def _apoll(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _gone(pid: int, timeout: float = 5.0) -> bool:
    def dead() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False
    return _poll(dead, timeout)


_RECORDS: list[Path] = []


@pytest.fixture(autouse=True)
def _no_fake_left_running():
    """End any fake child or grandchild a test (or a failing test) leaves
    behind, so nothing outlives the run."""
    _RECORDS.clear()
    yield
    for path in _RECORDS:
        for line in _lines(path):
            for key in ("pid", "grandchild_pid"):
                pid = line.get(key)
                if pid:
                    for kill in (os.killpg, os.kill):
                        try:
                            kill(pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        alive = False
        for path in _RECORDS:
            for line in _lines(path):
                for key in ("pid", "grandchild_pid"):
                    pid = line.get(key)
                    if pid:
                        try:
                            os.kill(pid, 0)
                        except (ProcessLookupError, PermissionError):
                            continue
                        alive = True
                        # direct: a grandchild does not lead the group
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
        if not alive:
            break
        time.sleep(0.05)


def _add_session(work: Path, session_id: str) -> None:
    """One more session saved for the same working directory."""
    project = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / PROJECT_KEY
    (project / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(work)}) + "\n", encoding="utf-8")


SUCCESS_STREAM = [
    _event(type="user", session_id=SESSION_ID),
    _event(type="result", subtype="success", is_error=False, result="done",
           session_id=SESSION_ID),
    _event(type="system", subtype="turn_aborted"),
]


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stream: list[bytes],
           *, exit_code: int = 0, after: str = ""
           ) -> tuple[ClaudeHeadRuntime, Path, Path]:
    """A fake claude by path plus a temporary config directory holding a
    transcript that saves SESSION_ID in the working directory. The record
    file exists only if the fake was invoked; `after` selects the fake's
    mode after the stream (see FAKE_CLAUDE)."""
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake.chmod(0o755)
    stream_file = tmp_path / "stream.txt"
    stream_file.write_bytes(b"".join(line + b"\n" for line in stream))
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", str(stream_file))
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", str(exit_code))
    monkeypatch.setenv("FAKE_CLAUDE_GC_READY", str(tmp_path / "gc-ready"))
    if after:
        monkeypatch.setenv("FAKE_CLAUDE_AFTER", after)
    config = tmp_path / "claude-config"
    project = config / "projects" / PROJECT_KEY
    project.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    work = tmp_path / "work"
    work.mkdir()
    (project / f"{SESSION_ID}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(work)}) + "\n", encoding="utf-8")
    _RECORDS.append(record)
    return ClaudeHeadRuntime(executable=str(fake)), record, work


@pytest.mark.parametrize("prompt", ["--model opus", "-x"])
def test_prompt_is_data_after_the_separator(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: str) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt=prompt))
    assert turn == HeadTurn("done", SESSION_ID)
    argv = _lines(record)[0]["argv"]
    assert argv[-2:] == ["--", prompt]
    assert argv.count("--") == 1


def test_argv_carries_the_head_flags_and_no_run_flags(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    argv = _lines(record)[0]["argv"]
    assert argv[1] == "-p"
    assert argv[2:4] == ["--resume", SESSION_ID]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--disallowedTools") + 1] == TURN_DISALLOWED_TOOLS
    for flag in ("--system-prompt", "--allowedTools", "--model", "--effort",
                 "--mcp-config"):
        assert flag not in argv


def test_child_runs_in_the_resolved_directory_with_the_turn_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    link = tmp_path / "link-to-work"
    link.symlink_to(work)
    asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=link, prompt="hello"))
    recorded = _lines(record)[0]
    assert recorded["cwd"] == str(work.resolve())
    assert recorded["disable_background_tasks"] == "1"
    assert recorded["config_dir"] == str(tmp_path / "claude-config")


def test_error_during_execution_is_turn_failed(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [_event(type="result", subtype="error_during_execution", is_error=True,
                    errors=["No conversation found with session ID: " + SESSION_ID],
                    session_id=SESSION_ID)]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines, exit_code=1)
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.TURN_FAILED
    assert str(exc.value) == (
        "turn-failed: No conversation found with session ID: " + SESSION_ID)


def test_exit_zero_without_a_result_event_is_outcome_unknown(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [_event(type="system", subtype="init"),
             _event(type="user", session_id=SESSION_ID)]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines, exit_code=0)
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN


def test_final_result_event_is_found_and_stray_lines_ignored(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [
        b"this line is not json",
        b"[1, 2]",
        _event(type="result", subtype="success", is_error=False,
               result="early text", session_id=SESSION_ID),
        _event(type="user", session_id=SESSION_ID),
        _event(type="result", subtype="success", is_error=False, result="done",
               session_id=SESSION_ID),
        _event(type="system", subtype="turn_aborted"),
    ]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines)
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert turn == HeadTurn("done", SESSION_ID)


def test_a_line_far_beyond_the_default_limit_is_read(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    long_text = "x" * (1024 * 1024)
    lines = [_event(type="result", subtype="success", is_error=False,
                    result=long_text, session_id=SESSION_ID)]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines)
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert turn == HeadTurn(long_text, SESSION_ID)


def test_limit_text_with_exit_one_is_rate_limited(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [_event(type="result", is_error=True,
                    result="You've hit your limit; it resets at 5pm",
                    session_id=SESSION_ID)]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines, exit_code=1)
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.RATE_LIMITED


def test_transport_fault_with_exit_one_is_outcome_unknown(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [_event(type="result", is_error=True,
                    result="fetch failed: socket hang up",
                    session_id=SESSION_ID)]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines, exit_code=1)
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN


def test_refusals_come_before_any_process_starts(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)

    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id="not-an-id", cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.INVALID_SESSION
    assert not record.exists()

    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=NO_TRANSCRIPT_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.SESSION_NOT_SAVED_HERE
    assert not record.exists()

    project = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / PROJECT_KEY
    (project / f"{OTHER_DIR_ID}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(tmp_path / "elsewhere")}) + "\n",
        encoding="utf-8")
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=OTHER_DIR_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.SESSION_NOT_SAVED_HERE
    assert not record.exists()


def test_child_stdin_is_not_inherited(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    parent_stdin = tmp_path / "parent-stdin"
    parent_stdin.write_bytes(b"inherited stdin")
    saved = os.dup(0)
    fd = os.open(parent_stdin, os.O_RDONLY)
    try:
        os.dup2(fd, 0)
        os.close(fd)
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    recorded = _lines(record)[0]
    assert recorded["stdin"] == "eof-at-once"


def test_returns_the_final_events_id_when_it_differs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [
        _event(type="user", session_id="first-seen-id"),
        _event(type="result", subtype="success", is_error=False, result="done",
               session_id="final-event-id"),
    ]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines)
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert turn == HeadTurn("done", "final-event-id")


def test_returns_the_first_id_seen_when_the_final_event_has_none(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [
        _event(type="user", session_id="first-seen-id"),
        _event(type="result", subtype="success", is_error=False, result="done"),
    ]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines)
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert turn == HeadTurn("done", "first-seen-id")


def test_returns_the_requested_id_when_no_event_carries_one(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = [_event(type="user"),
             _event(type="result", subtype="success", is_error=False, result="done")]
    runtime, record, work = _setup(monkeypatch, tmp_path, lines)
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert turn == HeadTurn("done", SESSION_ID)


def test_a_line_beyond_the_limit_ends_the_child_and_is_outcome_unknown(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(claude_head, "_LINE_LIMIT", 64)
    monkeypatch.setenv("FAKE_CLAUDE_AFTER", "sleep")
    runtime, record, work = _setup(monkeypatch, tmp_path, [b"x" * 200 + b"\n"])
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
    pid = _lines(record)[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_missing_executable_is_cli_unavailable(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    missing = ClaudeHeadRuntime(executable=str(tmp_path / "no-such-claude"))
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(missing.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.CLI_UNAVAILABLE


def test_transcript_recording_a_symlinked_spelling_is_accepted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    link = tmp_path / "link-to-work"
    link.symlink_to(work)
    transcript = (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / PROJECT_KEY
                  / f"{SESSION_ID}.jsonl")
    transcript.write_text(
        json.dumps({"type": "user", "cwd": str(link)}) + "\n", encoding="utf-8")
    turn = asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert turn == HeadTurn("done", SESSION_ID)


def test_deleted_saved_directory_is_refused_without_a_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    work.rmdir()
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.SESSION_NOT_SAVED_HERE
    assert not record.exists()


def test_turns_of_one_session_run_one_after_the_other(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")

    async def scenario() -> None:
        first = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        first_pid = _starts(record)[0]["pid"]
        second = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="two"))
        # the second turn waits: no second child while the first runs
        assert not await _apoll(lambda: len(_starts(record)) == 2, timeout=0.5)
        os.killpg(first_pid, signal.SIGTERM)
        with pytest.raises(HeadTurnError) as first_error:
            await asyncio.wait_for(first, 10)
        assert first_error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        # only now does the second turn start
        assert await _apoll(lambda: len(_starts(record)) == 2)
        second_pid = _starts(record)[1]["pid"]
        assert second_pid != first_pid
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, 10)
        assert _gone(first_pid) and _gone(second_pid)

    asyncio.run(scenario())


def test_turns_of_different_sessions_overlap(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")
    _add_session(work, OTHER_DIR_ID)

    async def scenario() -> None:
        one = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        two = asyncio.create_task(
            runtime.turn(session_id=OTHER_DIR_ID, cwd=work, prompt="two"))
        # both children run at once
        assert await _apoll(lambda: len(_starts(record)) == 2)
        pids = [line["pid"] for line in _starts(record)]
        assert pids[0] != pids[1]
        for pid in pids:
            os.killpg(pid, signal.SIGTERM)
        for task in (one, two):
            with pytest.raises(HeadTurnError) as error:
                await asyncio.wait_for(task, 10)
            assert error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        assert all(_gone(pid) for pid in pids)

    asyncio.run(scenario())


def test_aclose_ends_the_running_child_and_stops_the_waiting_turn(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")

    async def scenario() -> dict:
        running = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        waiting = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="two"))
        assert not await _apoll(lambda: len(_starts(record)) == 2, timeout=0.5)
        await runtime.aclose()
        with pytest.raises(HeadTurnError) as running_error:
            await asyncio.wait_for(running, 10)
        assert running_error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        with pytest.raises(HeadTurnError) as waiting_error:
            await asyncio.wait_for(waiting, 10)
        assert waiting_error.value.failure is HeadTurnFailure.STOPPING
        return start

    start = asyncio.run(scenario())
    assert _gone(start["pid"])
    assert _gone(start["grandchild_pid"])
    assert len(_starts(record)) == 1


def test_turn_after_aclose_raises_stopping_without_a_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    asyncio.run(runtime.aclose())
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.STOPPING
    assert not record.exists()


def test_cancelling_a_turn_ends_its_child_and_grandchild(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        return start

    start = asyncio.run(scenario())
    assert _gone(start["pid"])
    assert _gone(start["grandchild_pid"])


def test_aclose_twice_is_harmless(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        await runtime.aclose()
        await runtime.aclose()
        with pytest.raises(HeadTurnError):
            await asyncio.wait_for(task, 10)
        return start

    start = asyncio.run(scenario())
    assert _gone(start["pid"])
    assert _gone(start["grandchild_pid"])


def test_aclose_ends_a_sigterm_ignoring_grandchild(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="term-grand")
    # a pipe-holding grandchild keeps the ending's wait open until the
    # grace on 3.11, so the grace is short and the close bounded above
    # grace and margin together, as the other close tests are
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 2.0)

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        await asyncio.wait_for(runtime.aclose(), 8)
        with pytest.raises(HeadTurnError) as error:
            await asyncio.wait_for(task, 10)
        assert error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        return start

    start = asyncio.run(scenario())
    assert _gone(start["pid"])
    assert _gone(start["grandchild_pid"])


def test_a_child_ignoring_sigterm_is_killed_after_the_grace(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="stubborn")
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 0.3)

    async def scenario() -> int:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        pid = _starts(record)[0]["pid"]
        started = time.monotonic()
        await asyncio.wait_for(runtime.aclose(), 10)
        assert time.monotonic() - started >= 0.3
        with pytest.raises(HeadTurnError):
            await asyncio.wait_for(task, 10)
        return pid

    assert _gone(asyncio.run(scenario()))


def test_aclose_ends_children_of_several_sessions_together(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="stubborn")
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 0.3)
    second_id = "11111111-2222-4333-8444-555555555555"
    third_id = "22222222-3333-4444-8555-666666666666"
    _add_session(work, second_id)
    _add_session(work, third_id)

    async def scenario() -> tuple[float, list]:
        tasks = [asyncio.create_task(runtime.turn(session_id=sid, cwd=work,
                                                  prompt="x"))
                 for sid in (SESSION_ID, second_id, third_id)]
        assert await _apoll(lambda: len(_starts(record)) == 3)
        started = time.monotonic()
        await asyncio.wait_for(runtime.aclose(), 10)
        elapsed = time.monotonic() - started
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 10)
        return elapsed, results

    elapsed, results = asyncio.run(scenario())
    # all at once: about one grace, not one grace per child
    assert elapsed < 0.6
    assert all(isinstance(r, HeadTurnError)
               and r.failure is HeadTurnFailure.OUTCOME_UNKNOWN for r in results)


def test_a_stop_after_a_result_is_outcome_unknown(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="linger")

    async def scenario() -> int:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        pid = _starts(record)[0]["pid"]
        await asyncio.wait_for(runtime.aclose(), 10)
        with pytest.raises(HeadTurnError) as error:
            await asyncio.wait_for(task, 10)
        assert error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        return pid

    assert _gone(asyncio.run(scenario()))


def test_cancelling_during_the_final_wait_leaves_nothing(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="holdwait")
    waits: list[bool] = []
    real_wait = asyncio.subprocess.Process.wait

    async def spied_wait(self) -> int:
        waits.append(True)
        return await real_wait(self)

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", spied_wait)

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(
            lambda: any(line.get("phase") == "waiting" for line in _lines(record)))
        # the stream has ended and the runtime is inside proc.wait(); a
        # cancel that lands before this point ends a read, not the wait
        assert await _apoll(lambda: bool(waits)), \
            "proc.wait() was never entered"
        start = _starts(record)[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        return start

    start = asyncio.run(scenario())
    assert _gone(start["pid"])
    assert _gone(start["grandchild_pid"])


def test_a_line_beyond_the_limit_leaves_neither_child_nor_grandchild(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(claude_head, "_LINE_LIMIT", 64)
    runtime, record, work = _setup(monkeypatch, tmp_path, [b"x" * 200 + b"\n"],
                                   after="holdwait")
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
    start = _starts(record)[0]
    assert _gone(start["pid"])
    assert _gone(start["grandchild_pid"])


def test_an_invalid_session_id_creates_no_lock(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    with pytest.raises(HeadTurnError) as exc:
        asyncio.run(runtime.turn(session_id="not-an-id", cwd=work, prompt="hello"))
    assert exc.value.failure is HeadTurnFailure.INVALID_SESSION
    assert runtime._locks == {}


def _dead_now(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_aclose_waits_for_an_ending_already_in_flight(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="stubborn")
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 2.0)

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        task.cancel()
        await asyncio.sleep(0)  # the turn's ending starts
        await asyncio.wait_for(runtime.aclose(), 10)
        assert _dead_now(start["pid"]), "child alive when aclose returned"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        return start

    start = asyncio.run(scenario())
    assert _gone(start["grandchild_pid"], timeout=1.0)


def test_a_cancelled_aclose_leaves_the_ending_running(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="term-grand")
    # longer than any wait the test makes, so the grace never ends the
    # ending's window: only the test's release does
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 60.0)
    waits: list[bool] = []
    window = asyncio.Event()
    real_wait = asyncio.subprocess.Process.wait

    async def holding_wait(self) -> int:
        waits.append(True)
        await window.wait()  # every wait on a child is held until set
        return await real_wait(self)

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", holding_wait)

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        first = asyncio.create_task(runtime.aclose())
        try:
            # the ending has sent its SIGTERM — the child ends at it, the
            # grandchild ignores it — and is inside its held wait
            assert await _apoll(lambda: bool(waits))
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 10)
        finally:
            # a failing assertion above leaves no wait held
            window.set()
        await asyncio.wait_for(runtime.aclose(), 20)
        with pytest.raises(HeadTurnError) as error:
            await asyncio.wait_for(task, 20)
        assert error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        return start

    start = asyncio.run(scenario())
    # the grandchild ignored the SIGTERM: only the ending's group SIGKILL
    # ends it, so its death shows the ending ran on to its kill — a
    # close whose cancel cancelled the ending leaves it alive
    assert _gone(start["grandchild_pid"], timeout=1.0)


def test_a_stop_racing_the_spawn_is_outcome_unknown(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="serve")
    real_exec = asyncio.create_subprocess_exec
    spawned: list[int] = []
    stops: list[asyncio.Task] = []

    async def racing_exec(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc.pid)
        # the stop lands inside the spawn window, with the child created
        # but the spawn unreturned; a close awaited from here would wait
        # for the spawn it runs inside, so the stop runs as a task of
        # its own, and the window stays open a measurable while
        stops.append(asyncio.create_task(runtime.aclose()))
        await asyncio.sleep(1.0)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", racing_exec)

    async def scenario() -> int | None:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: bool(stops)), "the stop never started"
        # the close is awaited first: it must find the spawn in flight
        # and leave no child it was told of when it returns
        await asyncio.wait_for(stops[0], 20)
        assert _dead_now(spawned[0]), "child alive when the close ended"
        with pytest.raises(HeadTurnError) as error:
            await asyncio.wait_for(task, 10)
        assert error.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        # the child may be ended before the fake writes its record
        lines = _lines(record)
        return lines[0].get("grandchild_pid") if lines else None

    grandchild = asyncio.run(scenario())
    if grandchild:
        assert _gone(grandchild)


def test_a_turn_cancelled_during_its_spawn_ends_whole(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="serve")
    real_exec = asyncio.create_subprocess_exec
    spawned: list[int] = []
    window = asyncio.Event()

    async def spawning_exec(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc.pid)
        # the spawn window stays open until the test sets the event, so
        # no poll or stall can run past it
        await window.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawning_exec)

    async def scenario() -> None:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        try:
            # the fake's exec has been entered and its grandchild
            # started — the start line is written after the grandchild
            # exists — before anything else happens
            assert await _apoll(lambda: bool(_starts(record)))
            # the loop stalls inside the open spawn window, and the
            # cancel lands in it with the event unset
            time.sleep(0.5)
            task.cancel()
        finally:
            # set after the cancel, so the spawn returns the handle for
            # the turn's cleanup; a failing assertion above leaves no
            # wrapper holding the window
            window.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 20)
        # the whole group of the child the spawn yielded is ended, the
        # grandchild with it: the group id answers no probe
        def group_gone() -> bool:
            try:
                os.killpg(spawned[0], 0)
            except ProcessLookupError:
                return True
            return False

        assert _gone(spawned[0])
        assert _poll(group_gone), "a process of the child's group still lives"

    asyncio.run(scenario())


def test_a_turn_in_a_spawn_window_when_main_returns_leaves_no_child(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    real_exec = asyncio.create_subprocess_exec
    spawned: list[int] = []

    async def slow_exec(*args, **kwargs):
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc.pid)
        try:
            await asyncio.sleep(5)  # the spawn window outlives main
        except asyncio.CancelledError:
            # the loop's end takes the spawn with it; the child and its
            # transport are still only this wrapper's to end, as a
            # cancelled exec's own path ends them — and waits for the
            # end, so the child is gone before the loop can return
            getattr(proc, "_transport").close()
            await proc.wait()
            raise
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)

    async def main() -> None:
        asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: bool(spawned))

    started = time.monotonic()
    asyncio.run(main())  # the loop's end cancels the turn and its spawn
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the loop's end took {elapsed:.1f}s"
    # the child's liveness is read at the loop's return, without
    # blocking: a pid of zero is a child still running, a reaped result
    # or no child at all is not
    alive = False
    try:
        try:
            observed, _status = os.waitpid(spawned[0], os.WNOHANG)
            alive = observed == 0
        except ChildProcessError:
            pass  # already reaped, whatever watcher the interpreter ran
        assert not alive, "child alive after the loop's end"
    finally:
        if alive:
            # a child found running is ended and reaped whatever the
            # assertion said of it, and no wait of the test blocks
            try:
                os.kill(spawned[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    observed, _status = os.waitpid(spawned[0], os.WNOHANG)
                    if observed != 0:
                        break
                except (ProcessLookupError, ChildProcessError):
                    break
                time.sleep(0.02)


def test_a_cancel_during_a_failing_spawn_ends_cancelled(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    real_exec = asyncio.create_subprocess_exec
    entered: list[bool] = []
    window = asyncio.Event()

    async def failing_exec(*args, **kwargs):
        entered.append(True)
        # the spawn window stays open until the test sets the event, so
        # the failure waits for the cancel
        await window.wait()
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_exec)

    async def scenario() -> None:
        # the prompt holds a NUL: the exec itself raises ValueError
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="a\x00b"))
        try:
            # the turn is inside its spawn window, held open, before the
            # failure
            assert await _apoll(lambda: bool(entered))
            task.cancel()  # the cancel lands with the event unset
        finally:
            # set after the cancel, so the spawn fails with the
            # cancellation pending; a failing assertion above leaves no
            # wrapper holding the window
            window.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    asyncio.run(scenario())
    assert not record.exists()


def test_a_turn_cancelled_twice_leaves_no_child(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="stubborn")
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 2.0)

    async def scenario() -> dict:
        task = asyncio.create_task(
            runtime.turn(session_id=SESSION_ID, cwd=work, prompt="one"))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        start = _starts(record)[0]
        task.cancel()
        await asyncio.sleep(0)  # the first cancellation reaches the ending
        task.cancel()  # the second lands while the ending is awaited
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert _dead_now(start["pid"]), "child alive when the turn ended"
        return start

    start = asyncio.run(scenario())
    assert _gone(start["grandchild_pid"], timeout=1.0)


FRESH_ID = "0d9e6f21-3c4b-4a55-8d67-9e8f0a1b2c3d"
START_NAMES = ("FAKE_CLAUDE_RECORD", "FAKE_CLAUDE_STREAM", "FAKE_CLAUDE_EXIT",
               "FAKE_CLAUDE_GC_READY", "FAKE_CLAUDE_AFTER", "FAKE_CLAUDE_PLAIN",
               "CLAUDE_CONFIG_DIR", "SAKSHI_ABSENT_NAME")


def _start_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                 stream: list[bytes], *, after: str = ""
                 ) -> tuple[ClaudeHeadRuntime, Path, Path]:
    """The scaffolding of _setup with a runtime allowed to name the
    environment a fresh session is given, fake's own names included."""
    runtime, record, work = _setup(monkeypatch, tmp_path, stream, after=after)
    starter = ClaudeHeadRuntime(executable=str(tmp_path / "fake-claude"),
                                environment_names=START_NAMES)
    return starter, record, work


def test_start_argv_begins_a_fresh_session(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    turn = asyncio.run(starter.start(
        session_id=FRESH_ID, cwd=work, prompt="review it", model="opus",
        effort="xhigh", add_dirs=(checkout, memory)))
    assert turn == HeadTurn("done", SESSION_ID)
    argv = _lines(record)[0]["argv"]
    assert argv == [str(tmp_path / "fake-claude"), "-p",
                    "--session-id", FRESH_ID,
                    "--output-format", "stream-json",
                    "--verbose",
                    "--dangerously-skip-permissions",
                    "--disallowedTools", TURN_DISALLOWED_TOOLS,
                    "--model", "opus",
                    "--effort", "xhigh",
                    "--add-dir", str(checkout),
                    "--add-dir", str(memory),
                    "--", "review it"]
    assert starter._locks == {}


def _baseline_environment(fake: Path, work: Path,
                          names: tuple[str, ...]) -> None:
    """Run the same fake once under a clean environment of exactly the
    listed present names and TURN_ENVIRONMENT's; whatever the platform
    and the interpreter add lands in its recorded start line, which the
    runtime's child is then measured against."""
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(claude_head.TURN_ENVIRONMENT)
    subprocess.run([str(fake)], cwd=str(work), env=env, check=False,
                   stdin=subprocess.DEVNULL, capture_output=True)


def test_start_environment_matches_a_clean_baseline(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HIDDEN_FROM_THE_CHILD", "x")
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    _baseline_environment(tmp_path / "fake-claude", work, START_NAMES)
    asyncio.run(starter.start(session_id=FRESH_ID, cwd=work, prompt="hello",
                              model="opus", effort="high"))
    starts = _starts(record)
    assert len(starts) == 2
    assert starts[1]["environment"] == starts[0]["environment"]


def test_the_parent_sessions_messaging_and_identity_never_reach_the_child(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("CLAUDE_CODE_MESSAGING_SOCKET",
                 "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "planted")
    monkeypatch.setenv("LC_CTYPE", "a-value-no-interpreter-chooses")
    starter, record, work = _start_setup(monkeypatch, tmp_path,
                                         SUCCESS_STREAM)
    names = tuple(name for name in START_NAMES
                  if name != "CLAUDE_CONFIG_DIR")
    starter = ClaudeHeadRuntime(executable=str(tmp_path / "fake-claude"),
                                environment_names=names)
    _baseline_environment(tmp_path / "fake-claude", work, names)
    asyncio.run(starter.start(session_id=FRESH_ID, cwd=work, prompt="hello",
                              model="opus", effort="high"))
    starts = _starts(record)
    assert len(starts) == 2
    assert starts[1]["environment"] == starts[0]["environment"]


def test_turn_environment_wins_over_a_listed_name(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "planted")
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    starter = ClaudeHeadRuntime(
        executable=str(tmp_path / "fake-claude"),
        environment_names=START_NAMES + ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",))
    asyncio.run(starter.start(session_id=FRESH_ID, cwd=work, prompt="hello",
                              model="opus", effort="high"))
    environment = _starts(record)[0]["environment"]
    assert environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] ==         claude_head.TURN_ENVIRONMENT["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"]


def test_add_dirs_keep_their_given_order(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    asyncio.run(starter.start(session_id=FRESH_ID, cwd=work, prompt="hello",
                              model="opus", effort="high",
                              add_dirs=(memory, checkout)))
    argv = _starts(record)[0]["argv"]
    assert argv[-6:] == ["--add-dir", str(memory),
                         "--add-dir", str(checkout), "--", "hello"]


@pytest.mark.parametrize("detail", [
    "Error: Session ID 0d9e6f21-3c4b-4a55-8d67-9e8f0a1b2c3d is already in use.",
    "y" * 600,
])
def test_start_with_a_plain_line_and_no_event_is_a_failed_turn(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detail: str) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLAIN", detail)
    starter, record, work = _start_setup(monkeypatch, tmp_path,
                                         SUCCESS_STREAM, after="plain")
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(starter.start(session_id=FRESH_ID, cwd=work,
                                  prompt="hello", model="opus",
                                  effort="high"))
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    assert str(raised.value) == "turn-failed: " + detail[:500]


@pytest.mark.parametrize("detail", [
    "Error: Session ID 3f2b8a10-9c4d-4e5f-8a7b-1c2d3e4f5a6b is already in use.",
    "y" * 600,
])
def test_turn_with_a_plain_line_and_no_event_is_a_failed_turn(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detail: str) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLAIN", detail)
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM,
                                   after="plain")
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work,
                                 prompt="hello"))
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    assert str(raised.value) == "turn-failed: " + detail[:500]


def test_the_reader_keeps_only_a_prefix_of_a_plain_flood(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, [], after="flood")
    # trace only when the caller does not: PYTHONTRACEMALLOC=1 leaves a
    # tracing on whose baseline and frame depth are the caller's
    started_here = not tracemalloc.is_tracing()
    if started_here:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline = tracemalloc.get_traced_memory()[0]
        with pytest.raises(HeadTurnError) as raised:
            asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work,
                                     prompt="hello"))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if started_here:
            tracemalloc.stop()
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    # the first detail-limit characters of the plain lines joined by
    # newlines, exactly as a turn reading one plain line is detailed
    assert raised.value.detail == ("p" * 99 + "\n") * 5
    # about a hundred megabytes grew before; a prefix is a fraction of
    # this, whatever baseline the tracing began from
    assert peak - baseline < 10 * 1024 * 1024


def test_a_prompt_too_long_is_a_failed_turn_start(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(starter.start(session_id=FRESH_ID, cwd=work,
                                  prompt="x" * 2000000, model="opus",
                                  effort="high"))
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    assert str(raised.value) == "turn-failed: prompt too long"
    assert not record.exists()


def test_a_prompt_too_long_is_a_failed_turn_turn(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime, record, work = _setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(runtime.turn(session_id=SESSION_ID, cwd=work,
                                 prompt="x" * 2000000))
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    assert str(raised.value) == "turn-failed: prompt too long"
    assert not record.exists()


def test_start_returns_the_result_session_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = "11111111-2222-4333-8444-555555555555"
    stream = [_event(type="result", subtype="success", is_error=False,
                     result="done", session_id=other)]
    starter, record, work = _start_setup(monkeypatch, tmp_path, stream)
    turn = asyncio.run(starter.start(session_id=FRESH_ID, cwd=work,
                                     prompt="hello", model="opus",
                                     effort="high"))
    assert turn.session_id == other
    assert _starts(record)[0]["argv"][2:4] == ["--session-id", FRESH_ID]


def test_start_without_any_session_id_returns_the_requested(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stream = [_event(type="result", subtype="success", is_error=False,
                     result="done")]
    starter, _, work = _start_setup(monkeypatch, tmp_path, stream)
    turn = asyncio.run(starter.start(session_id=FRESH_ID, cwd=work,
                                     prompt="hello", model="opus",
                                     effort="high"))
    assert turn == HeadTurn("done", FRESH_ID)


def test_start_refuses_an_invalid_id_before_any_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(starter.start(session_id="not-an-id", cwd=work,
                                  prompt="hello", model="opus",
                                  effort="high"))
    assert raised.value.failure is HeadTurnFailure.INVALID_SESSION
    assert not record.exists()


def test_start_refuses_a_closed_runtime_before_any_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, work = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)

    async def scenario() -> None:
        await starter.aclose()
        with pytest.raises(HeadTurnError) as raised:
            await starter.start(session_id=FRESH_ID, cwd=work,
                                prompt="hello", model="opus", effort="high")
        assert raised.value.failure is HeadTurnFailure.STOPPING

    asyncio.run(scenario())
    assert not record.exists()


def test_start_refuses_a_missing_working_directory(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, _ = _start_setup(monkeypatch, tmp_path, SUCCESS_STREAM)
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(starter.start(session_id=FRESH_ID,
                                  cwd=tmp_path / "absent",
                                  prompt="hello", model="opus",
                                  effort="high"))
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    assert "working directory missing" in str(raised.value)
    assert not record.exists()


def test_a_stop_during_start_ends_the_child_group(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, work = _start_setup(monkeypatch, tmp_path,
                                         SUCCESS_STREAM, after="serve")

    async def scenario() -> None:
        task = asyncio.create_task(starter.start(
            session_id=FRESH_ID, cwd=work, prompt="hello", model="opus",
            effort="high"))
        assert await _apoll(lambda: bool(_starts(record)))
        started = _starts(record)[0]
        await starter.aclose()
        with pytest.raises(HeadTurnError) as raised:
            await asyncio.wait_for(task, 20)
        assert raised.value.failure is HeadTurnFailure.OUTCOME_UNKNOWN
        assert _gone(started["pid"])
        assert _gone(started["grandchild_pid"])

    asyncio.run(scenario())
    assert len(_starts(record)) == 1


def _sudo_lines(record: Path) -> list[dict]:
    return _lines(record)


def _sudo_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                stream: list[bytes], *, after: str = "", mode: str = "",
                ) -> tuple[ClaudeHeadRuntime, Path, Path, Path, str]:
    """The scaffolding of _start_setup with a fake sudo and the real
    /bin/kill, a runtime that starts a role as `role-x` through the
    fake, and the fake sudo's own record file."""
    runtime, record, work = _setup(monkeypatch, tmp_path, stream,
                                   after=after)
    fake_sudo = tmp_path / "fake-sudo"
    fake_sudo.write_text(FAKE_SUDO, encoding="utf-8")
    fake_sudo.chmod(0o755)
    sudo_record = tmp_path / "sudo-record.json"
    monkeypatch.setenv("FAKE_SUDO_RECORD", str(sudo_record))
    monkeypatch.setenv("FAKE_SUDO_MODE", mode)
    starter = ClaudeHeadRuntime(
        executable=str(tmp_path / "fake-claude"),
        environment_names=START_NAMES + ("FAKE_SUDO_RECORD",
                                         "FAKE_SUDO_MODE"),
        sudo=str(fake_sudo), kill="/bin/kill")
    return starter, record, sudo_record, work, "role-x"


def test_start_with_a_user_runs_through_sudo(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM)

    async def scenario() -> None:
        await starter.start(session_id=FRESH_ID, cwd=work,
                            prompt="review it", model="opus",
                            effort="high", user=user)

    asyncio.run(scenario())
    lines = [line for line in _sudo_lines(sudo_record)
             if line["argv"][5] != "/bin/kill"]
    assert len(lines) == 1
    assert lines[0]["user"] == user
    assert lines[0]["argv"] == [
        "-n", "-u", user, "-H", "--",
        str(tmp_path / "fake-claude"),
        "-p", "--session-id", FRESH_ID,
        "--output-format", "stream-json",
        "--verbose", "--dangerously-skip-permissions",
        "--disallowedTools", TURN_DISALLOWED_TOOLS,
        "--model", "opus", "--effort", "high",
        "--", "review it",
    ]
    # the fake claude's argv carries its own path first; sudo's does not
    assert _starts(record)[0]["argv"][1:] == lines[0]["argv"][6:]


def test_an_invalid_user_starts_no_process(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM)
    with pytest.raises(ValueError):
        asyncio.run(starter.start(session_id=FRESH_ID, cwd=work,
                                  prompt="review it", model="opus",
                                  effort="high", user="Not A User"))
    assert not sudo_record.exists()
    assert not record.exists()


def test_a_refusing_sudo_is_a_failed_turn(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM, mode="refuse")
    with pytest.raises(HeadTurnError) as raised:
        asyncio.run(starter.start(session_id=FRESH_ID, cwd=work,
                                  prompt="review it", model="opus",
                                  effort="high", user=user))
    assert raised.value.failure is HeadTurnFailure.TURN_FAILED
    assert "sudo: a password is required" in str(raised.value)


def test_a_stop_ends_the_roles_group_as_the_user(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")

    async def scenario() -> HeadTurnError:
        task = asyncio.create_task(
            starter.start(session_id=FRESH_ID, cwd=work,
                          prompt="review it", model="opus",
                          effort="high", user=user))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        await starter.aclose()
        with pytest.raises(HeadTurnError) as raised:
            await task
        return raised.value

    error = asyncio.run(scenario())
    assert error.failure is HeadTurnFailure.OUTCOME_UNKNOWN
    # the fake sudo execs the fake claude, so its pid is the group id
    pid = _starts(record)[0]["pid"]
    grandchild = _starts(record)[0]["grandchild_pid"]
    assert _gone(pid)
    assert _gone(grandchild)
    kills = [line for line in _sudo_lines(sudo_record)
             if line["argv"][5] == "/bin/kill"]
    assert any(line["argv"][6:] == ["-TERM", "--", f"-{pid}"]
               for line in kills)
    assert any(line["argv"][6:] == ["-KILL", "--", f"-{pid}"]
               for line in kills)


def test_a_group_only_the_role_could_end_is_named_and_left(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(claude_head, "_CLOSE_GRACE", 1.0)
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM, after="serve",
        mode="dummy")

    async def scenario() -> HeadTurnError:
        task = asyncio.create_task(
            starter.start(session_id=FRESH_ID, cwd=work,
                          prompt="review it", model="opus",
                          effort="high", user=user))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        started = time.monotonic()
        await starter.aclose()
        assert time.monotonic() - started < 8.0
        pid = _sudo_lines(sudo_record)[0]["pid"]
        written = capsys.readouterr().err
        assert user in written and str(pid) in written
        grandchild = _starts(record)[0]["grandchild_pid"]
        # sudo itself ended by the direct KILL; its group lives on
        assert _gone(pid)
        assert not _gone(grandchild)
        try:
            os.killpg(pid, 0)
        except OSError:
            raise AssertionError("the role's group was ended")
        # the group is left to the role's user; the test ends it itself
        os.killpg(pid, signal.SIGKILL)
        with pytest.raises(HeadTurnError) as raised:
            await task
        return raised.value

    error = asyncio.run(scenario())
    assert error.failure is HeadTurnFailure.OUTCOME_UNKNOWN


def _probe_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the module's os.killpg answer a probe of a group held by
    another user the way the platform does — a permission error — and
    delegate every other signal to the real call."""
    real_killpg = os.killpg

    def killpg(pid: int, sig: int) -> None:
        if sig == 0:
            raise PermissionError(1, "Operation not permitted")
        return real_killpg(pid, sig)

    monkeypatch.setattr(claude_head.os, "killpg", killpg)


def test_a_group_answering_permission_is_named_on_a_stop(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture) -> None:
    _probe_refuses(monkeypatch)
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM, after="serve")

    async def scenario() -> tuple[HeadTurnFailure, int]:
        task = asyncio.create_task(
            starter.start(session_id=FRESH_ID, cwd=work,
                          prompt="review it", model="opus",
                          effort="high", user=user))
        assert await _apoll(lambda: len(_starts(record)) == 1)
        # no error leaves the close
        await asyncio.wait_for(starter.aclose(), 20)
        with pytest.raises(HeadTurnError) as raised:
            await asyncio.wait_for(task, 20)
        return raised.value.failure, _starts(record)[0]["pid"]

    failure, pid = asyncio.run(scenario())
    assert failure is HeadTurnFailure.OUTCOME_UNKNOWN
    written = capsys.readouterr().err
    assert f"user {user}" in written
    assert f"group {pid}" in written


def test_a_group_answering_permission_keeps_a_completed_turns_result(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture) -> None:
    _probe_refuses(monkeypatch)
    starter, record, sudo_record, work, user = _sudo_setup(
        monkeypatch, tmp_path, SUCCESS_STREAM)

    async def scenario() -> tuple[HeadTurn, int]:
        turn = await starter.start(session_id=FRESH_ID, cwd=work,
                                   prompt="review it", model="opus",
                                   effort="high", user=user)
        return turn, _starts(record)[0]["pid"]

    turn, pid = asyncio.run(scenario())
    assert turn == HeadTurn("done", SESSION_ID)
    written = capsys.readouterr().err
    assert f"user {user}" in written
    assert f"group {pid}" in written

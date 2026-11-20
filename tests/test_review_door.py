"""The review door: one socket, one line in, one answer out, and the
command that reaches it."""

import asyncio
import json
import shutil
import socket
import stat
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

import orchestrator.review_door as review_door
from orchestrator.review_door import (
    SOCKET_NAME,
    ReviewDoorUnavailable,
    ask,
    serve,
)
from orchestrator.review_intake import ReviewIntake
from orchestrator.reviews import ReviewAnswer, ReviewRefusal, ReviewRequest
from orchestrator.sakshi_settings import (
    ReviewedRepository,
    ReviewTeam,
    RoleSettings,
    SakshiSettings,
)

REPO = Path(__file__).resolve().parent.parent
COMMIT = "abcdef0123456789abcdef0123456789abcdef12"
FIELDS: dict[str, object] = {
    "repository": "owner/name",
    "number": 7,
    "head_commit": COMMIT,
    "head_repository": "owner/name",
}
INVALID_LINE = b'{"accepted": false, "refusal": "invalid-request"}\n'
ACCEPTED_LINE = b'{"accepted": true}\n'


@pytest.fixture
def short_dirs():
    """Short-lived directories under /tmp; pytest's own paths are long
    enough that the socket's name would not fit."""
    made: list[Path] = []

    def make() -> Path:
        path = Path(tempfile.mkdtemp(dir="/tmp"))
        made.append(path)
        return path

    yield make
    for path in made:
        shutil.rmtree(path, ignore_errors=True)


def _role() -> RoleSettings:
    return RoleSettings(seed=Path("/srv/seeds/architect"),
                        user="review_arch", model="claude-3",
                        effort="high", head="01")


def _intake(directory: Path, *, key: str = "owner/name",
            roles: bool = True) -> tuple[ReviewIntake, list[object]]:
    calls: list[object] = []
    team = ReviewTeam(memory=Path("/srv/memory"),
                      deadline_seconds=30,
                      roles={"architect": _role()} if roles else {},
                      musafir=_role())
    settings = SakshiSettings(
        workspace_root=Path("/srv/sakshi"),
        repositories={key: ReviewedRepository(
            checkout=Path("/srv/checkouts/name"), team=team)})
    return ReviewIntake(settings, directory, calls.append), calls


def _raw_exchange(directory: Path, payload: bytes | None,
                  timeout: float = 5.0) -> bytes:
    path = str(directory / SOCKET_NAME)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path)
        if payload is not None:
            if payload:
                client.sendall(payload)
            else:
                client.shutdown(socket.SHUT_WR)
        return client.recv(4096)


async def _serving(directory: Path, intake: ReviewIntake):
    door = serve(directory, intake)
    task = asyncio.create_task(door.serve())
    return door, task


async def _ended(task: asyncio.Task) -> None:
    """The door's loop ends once aclose() closes the listener, quietly."""
    for _ in range(100):
        if task.done():
            await asyncio.wait_for(task, 5)
            assert not task.cancelled()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the door's serve loop did not end")


def _padded_line(total: int) -> bytes:
    body = json.dumps(FIELDS).encode()
    return b" " * (total - len(body) - 1) + body + b"\n"


def test_a_request_reaches_admit_and_is_answered(short_dirs) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            answer = await asyncio.to_thread(ask, directory, FIELDS)
            assert answer == ReviewAnswer(refusal=None)
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert calls[0].request == ReviewRequest(
        repository="owner/name", number=7, head_commit=COMMIT,
        head_repository="owner/name")
    assert calls[0].checkout == Path("/srv/checkouts/name")


REFUSALS = [
    ("stopping", dict(stop=True), ReviewRefusal.STOPPING),
    ("unreviewed", dict(repository="other/repo",
                        head_repository="other/repo"),
     ReviewRefusal.REPOSITORY_NOT_REVIEWED),
    ("fork", dict(head_repository="other/fork"),
     ReviewRefusal.HEAD_FROM_ANOTHER_REPOSITORY),
    ("incomplete", dict(no_roles=True), ReviewRefusal.ROLES_INCOMPLETE),
]


@pytest.mark.parametrize(("name", "options", "refusal"), REFUSALS)
def test_every_refusal_crosses_the_wire(short_dirs, name, options,
                                        refusal) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs(),
                            roles=not options.get("no_roles", False))
    fields = {**FIELDS}
    for key in ("repository", "head_repository"):
        if key in options:
            fields[key] = options[key]
    if options.get("stop"):
        intake.stop()

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            answer = await asyncio.to_thread(ask, directory, fields)
            assert answer == ReviewAnswer(refusal=refusal)
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert calls == []


BAD_LINES = [
    b"x" * (64 * 1024 + 1) + b"\n",
    b"x" * (64 * 1024) + b"\n",
    b"not json\n",
    b"[1, 2]\n",
    b'{"repository": "owner/name", "number": 1, "head_commit": "'
    + COMMIT.encode() + b'", "head_repository": "owner/name", "team": "x"}\n',
    b"\xff\xfe{}\n",
    b"",
    b"[" * 32000 + b"]" * 32000 + b"\n",
]


@pytest.mark.parametrize("payload", BAD_LINES)
def test_hostile_lines_are_refused(short_dirs, capsys, payload) -> None:
    directory = short_dirs()
    intake, _ = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            reply = await asyncio.to_thread(_raw_exchange, directory,
                                            payload)
            assert reply == INVALID_LINE
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert capsys.readouterr() == ("", "")


def test_a_silent_client_is_refused(short_dirs, monkeypatch) -> None:
    monkeypatch.setattr(review_door, "_READ_SECONDS", 0.2)
    directory = short_dirs()
    intake, _ = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            reply = await asyncio.to_thread(_raw_exchange, directory, None,
                                            timeout=5.0)
            assert reply == INVALID_LINE
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())


def test_a_stale_socket_file_is_replaced(short_dirs) -> None:
    directory = short_dirs()
    path = directory / SOCKET_NAME
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.is_socket()
    serve(directory, _intake(short_dirs())[0])
    assert path.is_socket()


def test_a_regular_file_of_that_name_is_refused(short_dirs) -> None:
    directory = short_dirs()
    path = directory / SOCKET_NAME
    path.write_text("not a socket")
    with pytest.raises(SystemExit) as raised:
        serve(directory, _intake(short_dirs())[0])
    assert str(path) in str(raised.value)


def test_the_socket_is_owner_only_and_closes_cleanly(short_dirs) -> None:
    directory = short_dirs()
    path = directory / SOCKET_NAME
    door = serve(directory, _intake(short_dirs())[0])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    door.aclose()
    assert not path.exists()


def test_a_path_too_long_to_bind_is_refused(short_dirs) -> None:
    directory = short_dirs() / ("x" * 100)
    directory.mkdir()
    with pytest.raises(SystemExit) as raised:
        serve(directory, _intake(short_dirs())[0])
    assert str(directory / SOCKET_NAME) in str(raised.value)


def test_ask_without_a_listener_is_unavailable(short_dirs) -> None:
    directory = short_dirs()
    with pytest.raises(ReviewDoorUnavailable) as raised:
        ask(directory, FIELDS)
    assert str(directory / SOCKET_NAME) in str(raised.value)


class _GarbageServer:
    """A server at the socket's name that behaves however a test needs:
    it answers with a fixed line, never answers, closes silently, or
    holds the line open without an end."""

    def __init__(self, directory: Path, mode: str,
                 reply: bytes = b"garbage\n") -> None:
        self._directory = directory
        self._mode = mode
        self._reply = reply

    async def __aenter__(self) -> None:
        async def handler(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            if self._mode == "never":
                await reader.read()
            if self._mode == "answer" or self._mode == "hold":
                writer.write(self._reply)
                await writer.drain()
                if self._mode == "hold":
                    await reader.read()
            writer.close()

        self._server = await asyncio.start_unix_server(
            handler, path=str(self._directory / SOCKET_NAME))

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()


BAD_REPLIES = [
    b"garbage\n",
    b'{"accepted": false, "refusal": "nonsense"}\n',
    b'{"accepted": true, "extra": 1}\n',
    b'{"accepted": false, "refusal": "stopping", "extra": 1}\n',
    b'{"accepted": 5, "refusal": "stopping"}\n',
    b'{"accepted": 1}\n',
    b'{"accepted": 1.0}\n',
    b'{"accepted": true}',
    b'{"accepted": false, "refusal": "invalid-request"}',
]


@pytest.mark.parametrize("reply", BAD_REPLIES)
def test_a_reply_outside_the_form_is_unavailable(short_dirs,
                                                 reply) -> None:
    directory = short_dirs()

    async def scenario() -> None:
        async with _GarbageServer(directory, "answer", reply):
            with pytest.raises(ReviewDoorUnavailable) as raised:
                await asyncio.to_thread(ask, directory, FIELDS)
            assert str(directory / SOCKET_NAME) in str(raised.value)

    asyncio.run(scenario())


def test_a_reply_the_decoder_cannot_read_is_unavailable(
        short_dirs, monkeypatch) -> None:
    directory = short_dirs()
    reply = b"[" * 1000 + b"]" * 1000 + b"\n"

    def refusing(text: str) -> object:
        raise RecursionError("too deep for this interpreter")

    monkeypatch.setattr(
        review_door, "json",
        types.SimpleNamespace(loads=refusing, dumps=json.dumps))

    async def scenario() -> None:
        async with _GarbageServer(directory, "answer", reply):
            with pytest.raises(ReviewDoorUnavailable) as raised:
                await asyncio.to_thread(ask, directory, FIELDS)
            assert str(directory / SOCKET_NAME) in str(raised.value)

    asyncio.run(scenario())


def test_an_acceptance_line_is_an_acceptance(short_dirs) -> None:
    directory = short_dirs()

    async def scenario() -> ReviewAnswer:
        async with _GarbageServer(directory, "answer", ACCEPTED_LINE):
            return await asyncio.to_thread(ask, directory, FIELDS)

    answer = asyncio.run(scenario())
    assert answer == ReviewAnswer(refusal=None)
    assert answer.accepted


def test_a_reply_ended_by_a_newline_at_the_limit_is_an_acceptance(
        short_dirs) -> None:
    at_limit_dir = short_dirs()
    past_limit_dir = short_dirs()
    prefix = b'{"accepted": true}'
    at_limit = prefix + b" " * (4096 - len(prefix)) + b"\n"
    past_limit = prefix + b" " * (4097 - len(prefix)) + b"\n"

    async def scenario():
        async with _GarbageServer(at_limit_dir, "answer", at_limit):
            answer = await asyncio.to_thread(ask, at_limit_dir, FIELDS)
        async with _GarbageServer(past_limit_dir, "answer", past_limit):
            with pytest.raises(ReviewDoorUnavailable) as raised:
                await asyncio.to_thread(ask, past_limit_dir, FIELDS)
        return answer, str(raised.value)

    answer, unavailable = asyncio.run(scenario())
    assert answer == ReviewAnswer(refusal=None)
    assert answer.accepted
    assert str(past_limit_dir / SOCKET_NAME) in unavailable


def test_a_reply_past_the_limit_without_an_end_is_unavailable(
        short_dirs) -> None:
    directory = short_dirs()
    reply = b'{"accepted": ' + b"1" * 5000

    async def scenario() -> None:
        async with _GarbageServer(directory, "hold", reply):
            start = time.monotonic()
            with pytest.raises(ReviewDoorUnavailable):
                await asyncio.to_thread(ask, directory, FIELDS,
                                        timeout=10.0)
            assert time.monotonic() - start < 5

    asyncio.run(scenario())


def test_ask_times_out_when_nothing_answers(short_dirs) -> None:
    directory = short_dirs()

    async def scenario() -> None:
        async with _GarbageServer(directory, "never"):
            start = time.monotonic()
            with pytest.raises(ReviewDoorUnavailable) as raised:
                await asyncio.wait_for(
                    asyncio.to_thread(ask, directory, FIELDS,
                                      timeout=0.5), 10)
            assert time.monotonic() - start < 5
            assert str(directory / SOCKET_NAME) in str(raised.value)

    asyncio.run(scenario())


def test_ask_fails_when_the_server_closes_silently(short_dirs) -> None:
    directory = short_dirs()

    async def scenario() -> None:
        async with _GarbageServer(directory, "silent"):
            with pytest.raises(ReviewDoorUnavailable):
                await asyncio.to_thread(ask, directory, FIELDS)

    asyncio.run(scenario())


async def _run_command(argv: list[str]) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=str(REPO), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    return process.returncode or 0, stdout, stderr


def _review_command(state_dir: Path, *, before: bool = False,
                    repository: str = "owner/name",
                    number: str = "7") -> list[str]:
    argv = [sys.executable, "-m", "orchestrator.sakshi"]
    if before:
        argv += ["--state-dir", str(state_dir)]
    argv += ["review", repository, number, "--head-commit", COMMIT,
             "--head-repository", "owner/name"]
    if not before:
        argv += ["--state-dir", str(state_dir)]
    return argv


def test_the_command_answers_over_the_door(short_dirs) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            code, stdout, stderr = await _run_command(
                _review_command(directory))
            assert (code, stdout, stderr) == (0, b"", b"")
            refused = await _run_command(
                _review_command(directory, before=True,
                                repository="other/repo"))
            assert refused == (1, b"", b"repository-not-reviewed\n")
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert calls[0].request.number == 7


def test_the_command_exits_4_when_nothing_listens(short_dirs) -> None:
    directory = short_dirs()

    async def scenario() -> tuple[int, bytes, bytes]:
        return await _run_command(_review_command(directory, before=True))

    code, stdout, stderr = asyncio.run(scenario())
    assert code == 4
    assert stdout == b""
    assert str(directory / SOCKET_NAME).encode() in stderr


@pytest.mark.parametrize("missing", ["--head-commit", "--head-repository"])
def test_a_missing_head_flag_is_a_usage_error(short_dirs,
                                              missing) -> None:
    directory = short_dirs()
    argv = [sys.executable, "-m", "orchestrator.sakshi", "review",
            "owner/name", "7", "--head-repository", "owner/name",
            "--state-dir", str(directory)]
    if missing == "--head-commit":
        pass
    else:
        argv = [sys.executable, "-m", "orchestrator.sakshi", "review",
                "owner/name", "7", "--head-commit", COMMIT,
                "--state-dir", str(directory)]

    async def scenario() -> int:
        code, _, _ = await _run_command(argv)
        return code

    assert asyncio.run(scenario()) == 2


def test_an_oversize_request_through_the_command_exits_1(
        short_dirs) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs())
    argv = [sys.executable, "-m", "orchestrator.sakshi", "review",
            "owner/name", "7", "--head-commit", "a" * 100000,
            "--head-repository", "owner/name",
            "--state-dir", str(directory)]

    async def scenario() -> tuple[int, bytes, bytes]:
        door, task = await _serving(directory, intake)
        try:
            return await _run_command(argv)
        finally:
            door.aclose()
            await _ended(task)

    code, stdout, stderr = asyncio.run(scenario())
    assert (code, stdout, stderr) == (1, b"", b"invalid-request\n")
    assert calls == []


def test_a_cancel_of_the_serve_task_from_outside_is_raised(
        short_dirs) -> None:
    directory = short_dirs()
    intake, _ = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() is True
        door.aclose()

    asyncio.run(scenario())
    assert not (directory / SOCKET_NAME).exists()


def test_a_line_of_exactly_the_limit_is_accepted(short_dirs) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            reply = await asyncio.to_thread(
                _raw_exchange, directory, _padded_line(64 * 1024))
            assert reply == ACCEPTED_LINE
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert len(calls) == 1


def test_a_line_one_byte_past_the_limit_is_refused(short_dirs) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs())

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            reply = await asyncio.to_thread(
                _raw_exchange, directory, _padded_line(64 * 1024 + 1))
            assert reply == INVALID_LINE
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert calls == []


def test_a_newline_free_overrun_is_refused_at_once(short_dirs) -> None:
    directory = short_dirs()
    intake, _ = _intake(short_dirs())
    payload = b"x" * (64 * 1024 + 100)

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            reply = await asyncio.to_thread(_raw_exchange, directory,
                                            payload, timeout=3.0)
            assert reply == INVALID_LINE
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())


def test_a_client_that_sends_late_is_answered(short_dirs) -> None:
    directory = short_dirs()
    intake, _ = _intake(short_dirs())

    def late_exchange() -> bytes:
        path = str(directory / SOCKET_NAME)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(path)
            time.sleep(0.5)
            client.sendall((json.dumps(FIELDS) + "\n").encode())
            return client.recv(4096)

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            reply = await asyncio.to_thread(late_exchange)
            assert reply == ACCEPTED_LINE
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())


def test_the_connection_is_closed_after_the_line(short_dirs) -> None:
    directory = short_dirs()
    intake, _ = _intake(short_dirs())

    def whole_stream() -> tuple[bytes, bytes]:
        path = str(directory / SOCKET_NAME)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(path)
            client.sendall((json.dumps(FIELDS) + "\n").encode())
            return client.recv(4096), client.recv(4096)

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            first, second = await asyncio.to_thread(whole_stream)
            assert first == ACCEPTED_LINE
            assert second == b""
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())


def test_an_early_answer_while_the_client_still_sends(short_dirs) -> None:
    directory = short_dirs()
    intake, calls = _intake(short_dirs())
    huge = {**FIELDS, "head_commit": "a" * 3000000}

    async def scenario() -> None:
        door, task = await _serving(directory, intake)
        try:
            answer = await asyncio.to_thread(ask, directory, huge)
            assert answer == ReviewAnswer(
                refusal=ReviewRefusal.INVALID_REQUEST)
        finally:
            door.aclose()
            await _ended(task)

    asyncio.run(scenario())
    assert calls == []

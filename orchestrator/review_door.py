"""The review door: the socket and its wire, the boundary a request
crosses into the intake.

Standard library only, beside ``reviews``, whose request form parses
the wire's object, and the intake, whose answers cross back. Only a
refusal's value ever crosses the wire; a detail, which may carry the
sender's own text, never reaches the wire, standard error or a log.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Mapping
from pathlib import Path

from .review_intake import ReviewIntake
from .reviews import (
    ReviewAnswer,
    ReviewRefusal,
    ReviewRefused,
    parse_review_request,
)

SOCKET_NAME = "review.sock"
_LINE_LIMIT = 64 * 1024
_READ_SECONDS = 10.0
_REPLY_LIMIT = 4096


class ReviewDoorUnavailable(Exception):
    """The review door cannot be reached, or its reply is not the
    answer's form."""


class ReviewDoor:
    """One bound review socket: one request per connection, answered
    with one line, until aclose() closes the listener and removes the
    file."""

    def __init__(self, listener: socket.socket, path: Path,
                 intake: ReviewIntake) -> None:
        self._listener = listener
        self._path = path
        self._intake = intake
        self._server: asyncio.AbstractServer | None = None

    async def serve(self) -> None:
        """Answer connections until aclose() closes the listener."""
        server = await asyncio.start_unix_server(
            self._answer, sock=self._listener, limit=_LINE_LIMIT + 1)
        self._server = server
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # aclose() closed the server and ended the loop; that is
            # the door's quiet way out, and only that one.
            return
        finally:
            self._server = None

    def aclose(self) -> None:
        """Close the listener and remove the socket file."""
        if self._server is not None:
            self._server.close()
        else:
            self._listener.close()
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    async def _answer(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            line = await self._read_line(reader)
            answer = self._decide(line)
            await self._reply(writer, answer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _read_line(self, reader: asyncio.StreamReader) -> bytes:
        """One line of at most the decided limit within the decided
        seconds; every other read is an empty line, which refuses."""
        try:
            return await asyncio.wait_for(reader.readline(),
                                          _READ_SECONDS)
        except (TimeoutError, ValueError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError):
            return b""

    def _decide(self, line: bytes) -> ReviewAnswer:
        invalid = ReviewAnswer(refusal=ReviewRefusal.INVALID_REQUEST)
        if len(line) > _LINE_LIMIT:
            return invalid
        try:
            fields = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return invalid
        if not isinstance(fields, dict):
            return invalid
        try:
            request = parse_review_request(fields)
        except ReviewRefused:
            return invalid
        return self._intake.admit(request)

    async def _reply(self, writer: asyncio.StreamWriter,
                     answer: ReviewAnswer) -> None:
        if answer.accepted:
            payload: dict[str, object] = {"accepted": True}
        else:
            payload = {"accepted": False, "refusal": answer.refusal.value}
        try:
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
        except OSError:
            pass


def serve(directory: Path, intake: ReviewIntake) -> ReviewDoor:
    """Bind the review socket in the state directory and return the
    door.

    A stale socket file of that name is removed first; anything else
    of that name is refused. Sakshi holds its lock by then, so the
    socket replaced can never be a live Sakshi's. A path too long to
    bind is the operating system's error, reported naming the path.
    """
    path = directory / SOCKET_NAME
    if path.exists() or path.is_symlink():
        if path.is_socket():
            path.unlink()
        else:
            raise SystemExit(
                f"Cannot serve the review socket: {path} exists and is "
                f"not a socket")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen()
    except OSError as e:
        listener.close()
        raise SystemExit(f"Cannot bind the review socket {path}: {e}")
    os.chmod(path, 0o600)
    return ReviewDoor(listener, path, intake)


def ask(directory: Path, fields: Mapping[str, object],
        *, timeout: float = 10.0) -> ReviewAnswer:
    """Send one review request over the socket and read its answer.

    When the door cannot be reached — nothing listens, or the socket's
    path is too long — or its reply is not the answer's form,
    ReviewDoorUnavailable names the socket path.
    """
    path = directory / SOCKET_NAME
    line = _exchange(path, fields, timeout)
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReviewDoorUnavailable(
            f"the review door at {path} answered outside its form"
        ) from None
    if (isinstance(reply, dict) and set(reply) == {"accepted"}
            and reply["accepted"] is True):
        return ReviewAnswer(refusal=None)
    if (isinstance(reply, dict) and set(reply) == {"accepted", "refusal"}
            and reply["accepted"] is False
            and isinstance(reply["refusal"], str)):
        try:
            refusal = ReviewRefusal(reply["refusal"])
        except ValueError:
            raise ReviewDoorUnavailable(
                f"the review door at {path} answered outside its form"
            ) from None
        return ReviewAnswer(refusal=refusal)
    raise ReviewDoorUnavailable(
        f"the review door at {path} answered outside its form")


def _exchange(path: Path, fields: Mapping[str, object],
              timeout: float) -> bytes:
    """One line of the fields out, one line of the reply in. A server
    that answers before the whole line is sent — a line past its limit
    is refused that way — still has its answer read; a failure with
    nothing to read is unavailable."""
    payload = (json.dumps(fields) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        try:
            client.connect(str(path))
        except OSError as e:
            raise ReviewDoorUnavailable(
                f"cannot reach the review door at {path}: {e}") from e
        try:
            client.sendall(payload)
        except OSError:
            pass  # the answer may already be on its way; read it below
        received = bytearray()
        try:
            while b"\n" not in received:
                chunk = client.recv(4096)
                if not chunk:
                    break
                received += chunk
                if len(received) > _REPLY_LIMIT:
                    break
        except OSError as e:
            raise ReviewDoorUnavailable(
                f"cannot reach the review door at {path}: {e}") from e
    line = bytes(received).split(b"\n", 1)[0]
    if b"\n" not in received or len(line) > _REPLY_LIMIT:
        raise ReviewDoorUnavailable(
            f"the review door at {path} answered outside its form")
    return line

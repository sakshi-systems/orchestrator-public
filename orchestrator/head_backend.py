"""The head's backend: one self-contained message through the courier and a
wait, with no bound, for the answer deposited by the head's own command."""

from __future__ import annotations

import shlex
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import courier
from .errors import HeadNotReachedError
from .exchange import POLL_SECONDS, ExchangePaths, answer_command, exchange_paths, remove_call
from .locator import HeadIdentity
from .ports import AgentRole

_COVER = "You have a new assignment: read {message} and follow its instructions."


def how_to_answer(paths: ExchangePaths) -> str:
    """The `## How to answer` section of a message: the answer command and the
    call's answer path, the one home of the recipe's wording."""
    command = answer_command()
    return "\n".join([
        "## How to answer",
        "",
        f"Give the result to the command "
        f"`{shlex.quote(str(command))} {shlex.quote(str(paths.answer))}`, whose input is the "
        "result: pipe the result into that command. Your reply in this chat is not read. "
        "Where the instructions call the last message the result, this is it.",
        "",
    ])


class HeadAgentBackend:
    """A head as a runtime a role takes, or as the callee of a call that is no
    role: one message per call, one wait for its answer, and no process of the
    run's own.

    The call's three files live under the exchange placement. A call whose
    answer is already deposited returns it with no send; a call whose marker
    stands sends nothing and only waits; a fresh call writes the message, sends
    one sentence carrying its path through the courier, marks the send and
    waits. The wait polls for the deposit every POLL_SECONDS with no bound, and
    a call's files are removed once its answer is read, so nothing of a call
    outlives its answer. The session identity is `<owner>/<folder>`.
    """

    def __init__(self, identity: HeadIdentity, *, send=courier.send, sleep=time.sleep) -> None:
        self.identity = identity
        self._send = send
        self._sleep = sleep

    def run(
        self,
        *,
        role: AgentRole,
        prompt: str,
        cwd: str,
        instructions: str,
        session_id: str | None = None,
        exchange: str | None = None,
        bases: tuple[str, ...] | None = None,
    ) -> tuple[str, str]:
        if exchange is None:
            raise ValueError("exchange")
        directory, _, last = exchange.rpartition("/")
        paths = exchange_paths(Path(cwd), directory, f"{role.value}-{last}")
        return self._exchange(paths, lambda: self._write_message(paths, role, prompt, cwd, instructions))

    def call(
        self,
        project_dir: Path,
        directory: str,
        stem: str,
        compose: Callable[[ExchangePaths], str],
    ) -> tuple[str, str]:
        """The call of a head that is no role: `compose` builds the whole
        message from the call's paths, and the result is the answer's text with
        the session identity."""
        paths = exchange_paths(project_dir, directory, stem)

        def write() -> None:
            paths.message.write_text(compose(paths), encoding="utf-8")

        return self._exchange(paths, write)

    def _exchange(self, paths: ExchangePaths, write: Callable[[], None]) -> tuple[str, str]:
        """Send and wait: `write` lays the message down on a fresh call only."""
        paths.message.parent.mkdir(parents=True, exist_ok=True)
        if paths.answer.exists():
            return self._answer_of(paths)
        if not paths.sent.exists():
            write()
            result = self._send(self.identity, _COVER.format(message=paths.message))
            if not result.sent:
                raise HeadNotReachedError(
                    "A head could not be reached\n"
                    f"{self.identity.repository} {self.identity.owner} {self.identity.folder}\n"
                    f"{result.reason}"
                )
            paths.sent.write_text("", encoding="utf-8")
        while True:
            if paths.answer.exists():
                return self._answer_of(paths)
            self._sleep(POLL_SECONDS)

    def _answer_of(self, paths: ExchangePaths) -> tuple[str, str]:
        """Read a deposited answer, remove the call's files and return the text."""
        text = paths.answer.read_text(encoding="utf-8")
        remove_call(paths)
        return text, f"{self.identity.owner}/{self.identity.folder}"

    def _write_message(self, paths: ExchangePaths, role: AgentRole, prompt: str,
                       cwd: str, instructions: str) -> None:
        head = "\n".join([
            f"This is the {role.value} turn of the project at {cwd}.",
            "",
            "## Instructions",
            "",
            instructions,
            "",
            "## Assignment",
            "",
            prompt,
            "",
        ])
        paths.message.write_text(head + "\n" + how_to_answer(paths), encoding="utf-8")

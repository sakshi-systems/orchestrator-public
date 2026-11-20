"""A hosted head's turn as a port: the turn it returns and the failures a
sender branches on, by meaning.

Standard library only; imports nothing of the run, the flow or Sakshi's
root. The run's retry and exit handling stay with its own agent port — a
head's turn is never routed through it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple, Protocol


class HeadTurn(NamedTuple):
    """One finished turn of a hosted head: its text and its session id."""

    text: str
    session_id: str


class HeadTurnFailure(StrEnum):
    """The typed failures of a head's turn.

    The values are the contract a sender branches on, so they are part of
    the boundary and never respelled.
    """

    #: The session id is not valid; nothing ran.
    INVALID_SESSION = "invalid-session"
    #: The session was not saved for the working directory, or its place
    #: cannot be told; nothing ran.
    SESSION_NOT_SAVED_HERE = "session-not-saved-here"
    #: The runtime's executable cannot be started; nothing ran.
    CLI_UNAVAILABLE = "cli-unavailable"
    #: The person's login reached its limit, as the CLI reports it.
    RATE_LIMITED = "rate-limited"
    #: The CLI reported an error for the turn.
    TURN_FAILED = "turn-failed"
    #: The turn may have run and its result is lost; never resubmitted.
    OUTCOME_UNKNOWN = "outcome-unknown"
    #: The runtime is closing; the turn never started, so the sender may
    #: send it again.
    STOPPING = "stopping"


class HeadTurnError(Exception):
    """A failed head turn, carried by meaning.

    The message is the failure's value, followed by ": " and the detail
    when there is one.
    """

    def __init__(self, failure: HeadTurnFailure, detail: str | None = None) -> None:
        self.failure = failure
        self.detail = detail
        if detail:
            super().__init__(f"{failure}: {detail}")
        else:
            super().__init__(f"{failure}")


class HeadRuntimeProtocol(Protocol):
    """The runtime that continues a hosted head's existing session, one
    turn at a time, and ends cleanly."""

    async def turn(self, *, session_id: str, cwd: Path, prompt: str) -> HeadTurn:
        """Continue the session by its id from cwd, raising HeadTurnError
        when the turn fails."""
        ...

    async def start(self, *, session_id: str, cwd: Path, prompt: str,
                    model: str, effort: str,
                    add_dirs: Sequence[Path] = (),
                    user: str | None = None) -> HeadTurn:
        """Begin a session under an id the caller chose, running in the
        seed's own directory with the role's model and effort; a user,
        when one is given, starts the session as that OS user; never
        raises SESSION_NOT_SAVED_HERE."""
        ...

    async def aclose(self) -> None:
        """End the turns running and refuse new ones."""
        ...

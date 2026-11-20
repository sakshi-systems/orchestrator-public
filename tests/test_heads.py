"""A hosted head's turn port: the failure values a sender branches on and
the error that carries them."""

import pytest

from orchestrator.heads import HeadTurnError, HeadTurnFailure

VALUES = [
    (HeadTurnFailure.INVALID_SESSION, "invalid-session"),
    (HeadTurnFailure.SESSION_NOT_SAVED_HERE, "session-not-saved-here"),
    (HeadTurnFailure.CLI_UNAVAILABLE, "cli-unavailable"),
    (HeadTurnFailure.RATE_LIMITED, "rate-limited"),
    (HeadTurnFailure.TURN_FAILED, "turn-failed"),
    (HeadTurnFailure.OUTCOME_UNKNOWN, "outcome-unknown"),
    (HeadTurnFailure.STOPPING, "stopping"),
]


@pytest.mark.parametrize(("member", "value"), VALUES)
def test_failure_value(member: HeadTurnFailure, value: str) -> None:
    assert member.value == value
    assert str(member) == value
    # A sender branches on the raw string.
    assert member == value


def test_error_carries_failure_and_detail() -> None:
    error = HeadTurnError(HeadTurnFailure.SESSION_NOT_SAVED_HERE,
                          "no transcript for 3f2b8a10")
    assert error.failure is HeadTurnFailure.SESSION_NOT_SAVED_HERE
    assert error.detail == "no transcript for 3f2b8a10"
    assert str(error) == "session-not-saved-here: no transcript for 3f2b8a10"


def test_error_without_detail() -> None:
    error = HeadTurnError(HeadTurnFailure.STOPPING)
    assert error.failure is HeadTurnFailure.STOPPING
    assert error.detail is None
    assert str(error) == "stopping"

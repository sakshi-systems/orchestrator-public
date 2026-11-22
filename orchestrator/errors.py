"""The outcome exceptions: the vocabulary every layer raises and catches to signal how a run ends."""

from __future__ import annotations


class HaltError(Exception):
    """An operational halt that is not a task failure — 🟡.

    Every subclass and every raise site opens the message with a fixed
    clause naming what happened; anything variable — a path, a quoted
    line, an agent's own text, captured output — goes on a later line.
    The first line becomes the HALTED notification's whole detail, while
    the console keeps printing the message in full.
    """


class RateLimitError(HaltError):
    """Raised when the agent runtime reports exhausted usage quota."""


class NetworkError(HaltError):
    """Raised when a recognized transient infrastructure failure persists
    through the runtime adapter's bounded retries."""


class EmptyAnswerError(HaltError):
    """Raised when an agent ends its turn with an empty answer where the run
    writes the artifact from that answer."""


class BaseNotOnRemoteError(HaltError):
    """Raised when the commit a hosted role would read the project at is not
    on the repository's remote, so the role's own clone cannot reach it."""


class OriginNotFetchedError(HaltError):
    """Raised when the repository's remote could not be fetched, so nothing can
    be said of what its branches hold."""


class LeftoverBranchNotRemovedError(HaltError):
    """Raised when a branch an earlier attempt of the task may have pushed to
    the remote could not be removed, since keeping it would let the task's
    first call build on it silently."""


class BranchNotFetchedError(HaltError):
    """Raised when the implementer's branch could not be fetched from the
    remote, so none of his work can be laid as the run's tree."""


class BranchNotFromBaseError(HaltError):
    """Raised when the implementer's branch does not descend from the commit
    the run is at, so laying its tip could silently revert the base."""


class IterationCommitNotMadeError(HaltError):
    """Raised when the iteration's commit on the run's ref could not be made,
    so no reviewing role can be handed the iteration's state."""


class IterationRefNotPushedError(HaltError):
    """Raised when the iteration's ref could not be pushed to origin, so a
    reviewing role off the machine cannot read the iteration's state."""


class HeadNotReachedError(HaltError):
    """Raised when a head could not be reached — its address could not be read,
    or the courier could not deliver the call's message to it."""


class PipelineStopError(Exception):
    """Raised to request a graceful halt of the pipeline."""


class EscalationError(Exception):
    """Raised when an agent cannot produce its mandated output without a
    decision outside its authority. What escalation is lives in
    docs/concepts/outcomes.md, section "Escalation".

    A deliberate sibling of PipelineStopError, never a HaltError subclass:
    the base class a stop raises decides the outcome the stop reports.
    """

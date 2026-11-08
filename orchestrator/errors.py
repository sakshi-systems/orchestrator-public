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


class MissingArtifactError(HaltError):
    """Raised when an agent ends its turn without writing the artifact its step requires."""


class PipelineStopError(Exception):
    """Raised to request a graceful halt of the pipeline."""


class EscalationError(Exception):
    """Raised when an agent cannot honestly produce its mandated output because
    the missing decision is outside its authority — escalation, not halt.
    A deliberate sibling of PipelineStopError, NOT a HaltError subclass: halt is
    'not a task failure', escalation IS a judgment. Do not move it under HaltError."""

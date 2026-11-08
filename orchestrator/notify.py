"""Notification helpers — stdlib only, no new dependencies."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import OrchestratorConfig


# Alert types that report a task failure rather than a success.
_FAIL_ALERTS = {"task-fail"}

# Alert types that report an operational stop — not a task failure.
_HALT_ALERTS = {"stop"}

# Alert types that report an escalation — a judgment the run cannot make on its own.
_ESCALATION_ALERTS = {"escalation"}


class Outcome(Enum):
    """Every terminal state a run or a task can report through notify()."""
    TASK_DONE = "task_done"  # process_task: single task completed
    RUN_DONE = "run_done"  # _run_dynamic_loop: all tasks done
    MANUAL_STOP = "manual_stop"  # _run_dynamic_loop: state.stop_requested
    UNCONVERGED = "unconverged"  # cli(): PipelineStopError
    HALTED = "halted"  # cli(): HaltError
    ESCALATED = "escalated"  # cli(): EscalationError
    ERRORED = "errored"  # cli(): unrecognized Exception
    FORCE_QUIT = "force_quit"  # _handle_sigint: second Ctrl+C


_WORDS: dict[Outcome, str] = {
    Outcome.TASK_DONE: "Completed",
    Outcome.RUN_DONE: "Finished",
    Outcome.MANUAL_STOP: "Stopped",
    Outcome.UNCONVERGED: "Unconverged",
    Outcome.HALTED: "Halted",
    Outcome.ESCALATED: "Escalated",
    Outcome.ERRORED: "Errored",
    Outcome.FORCE_QUIT: "Force-quit",
}

_ALERT_TYPES: dict[Outcome, str] = {
    Outcome.TASK_DONE: "task",
    Outcome.RUN_DONE: "done",
    Outcome.MANUAL_STOP: "stop",
    Outcome.UNCONVERGED: "task-fail",
    Outcome.HALTED: "stop",
    Outcome.ESCALATED: "escalation",
    Outcome.ERRORED: "stop",
    Outcome.FORCE_QUIT: "stop",
}


def compose(outcome: Outcome, project: str, detail: str | None, run_summary: str) -> str:
    """Build the notification text: word, then project, then detail (if any), then run summary."""
    lines = [f"{_WORDS[outcome]}: {project}"]
    if detail:
        lines.append(detail)
    lines.append(run_summary)
    return "\n".join(lines)


def report(config: "OrchestratorConfig", outcome: Outcome, project: str, detail: str | None, run_summary: str) -> None:
    """Compose and send a notification for a run/task outcome."""
    notify(config, compose(outcome, project, detail, run_summary), _ALERT_TYPES[outcome])


def notify(config: "OrchestratorConfig", text: str, alert_type: str) -> None:
    """Send a Telegram notification if alert_type is listed in config.telegram_alerts."""
    if alert_type not in config.telegram_alerts:
        return
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return
    emoji = (
        "🔴" if alert_type in _FAIL_ALERTS
        else "🟡" if alert_type in _HALT_ALERTS
        else "🔵" if alert_type in _ESCALATION_ALERTS
        else "🟢"
    )
    send_telegram(config.telegram_bot_token, config.telegram_chat_id, f"{emoji} {text}")


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """POST a message to a Telegram chat via the Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data=payload, timeout=10)
    except Exception as e:
        print(f"  [telegram] notification failed: {e}")

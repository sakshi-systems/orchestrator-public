"""Unit tests for notify() — emoji-prefix mapping and telegram gating — and for the
composer (Outcome, compose(), report()) that builds every notification's text and
selects its alert type."""

import pytest

from orchestrator import notify as notify_module
from orchestrator.config import OrchestratorConfig
from orchestrator.notify import Outcome, compose, notify, report, _WORDS, _ALERT_TYPES


def _config(telegram_alerts, bot_token="t", chat_id="c") -> OrchestratorConfig:
    return OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        telegram_alerts=telegram_alerts,
    )


@pytest.fixture
def sent(monkeypatch):
    """Monkeypatch send_telegram with a recorder; never hits the network."""
    recorded = []

    def _fake_send_telegram(token, chat_id, text):
        recorded.append(text)

    monkeypatch.setattr(notify_module, "send_telegram", _fake_send_telegram)
    return recorded


# ---------------------------------------------------------------------------
# Task 1: Emoji-prefix mapping
# ---------------------------------------------------------------------------

ALL_ALERTS = ["task-fail", "stop", "task", "done", "whatever"]


def test_task_fail_alert_prefixed_red(sent):
    """Should prefix the message with 🔴 for alert_type 'task-fail'."""
    config = _config(ALL_ALERTS)
    notify(config, "some message", "task-fail")
    assert sent[0].startswith("🔴")


def test_stop_alert_prefixed_yellow(sent):
    """Should prefix the message with 🟡 for alert_type 'stop'."""
    config = _config(ALL_ALERTS)
    notify(config, "some message", "stop")
    assert sent[0].startswith("🟡")


def test_task_alert_prefixed_green(sent):
    """Should prefix the message with 🟢 for alert_type 'task'."""
    config = _config(ALL_ALERTS)
    notify(config, "some message", "task")
    assert sent[0].startswith("🟢")


def test_done_alert_prefixed_green(sent):
    """Should prefix the message with 🟢 for alert_type 'done'."""
    config = _config(ALL_ALERTS)
    notify(config, "some message", "done")
    assert sent[0].startswith("🟢")


def test_unknown_alert_prefixed_green(sent):
    """Should prefix the message with 🟢 for an unrecognized alert_type 'whatever'."""
    config = _config(ALL_ALERTS)
    notify(config, "some message", "whatever")
    assert sent[0].startswith("🟢")


def test_escalation_alert_prefixed_blue(sent):
    """Should prefix the message with 🔵 for alert_type 'escalation' — a distinct tier from
    the 🟢 default and the 🔴/🟡 failure/halt tiers."""
    config = _config(["escalation"])
    notify(config, "some message", "escalation")
    assert sent[0].startswith("🔵")


# ---------------------------------------------------------------------------
# Task 2: Gating (silent no-op)
# ---------------------------------------------------------------------------


def test_alert_type_not_listed_sends_nothing(sent):
    """Should send nothing when alert_type is not present in config.telegram_alerts."""
    config = _config(["task"])
    notify(config, "some message", "stop")
    assert sent == []


def test_missing_bot_token_sends_nothing(sent):
    """Should send nothing when telegram_bot_token is None, even though alert_type is listed and chat_id is set."""
    config = _config(["stop"], bot_token=None)
    notify(config, "some message", "stop")
    assert sent == []


def test_missing_chat_id_sends_nothing(sent):
    """Should send nothing when telegram_chat_id is None, even though alert_type is listed and token is set."""
    config = _config(["stop"], chat_id=None)
    notify(config, "some message", "stop")
    assert sent == []


# ---------------------------------------------------------------------------
# compose() — envelope order: word, project, detail (if any), run summary
# ---------------------------------------------------------------------------


def test_compose_with_detail_orders_word_project_detail_summary():
    """Should join word-and-project, then detail, then run summary, in that order, when
    detail is truthy."""
    text = compose(Outcome.HALTED, "myproject", "some detail", "Ran for 1m 0s · 2 tasks done")
    assert text == "Halted: myproject\nsome detail\nRan for 1m 0s · 2 tasks done"


def test_compose_without_detail_is_exactly_two_lines():
    """Should produce exactly two lines — word-and-project, then run summary — when
    detail is None, with no blank line or placeholder standing in for the missing detail."""
    text = compose(Outcome.ESCALATED, "myproject", None, "Ran for 1m 0s · 2 tasks done")
    assert text == "Escalated: myproject\nRan for 1m 0s · 2 tasks done"
    assert len(text.splitlines()) == 2


def test_compose_task_done_carries_run_summary():
    """Should include the run summary in TASK_DONE's composed text, which it did not
    before composition moved into one place."""
    text = compose(Outcome.TASK_DONE, "myproject", "Some task", "Ran for 1m 0s · 2 tasks done")
    assert "Ran for 1m 0s · 2 tasks done" in text


# ---------------------------------------------------------------------------
# _WORDS — one distinct word per Outcome
# ---------------------------------------------------------------------------


def test_words_covers_every_outcome_with_distinct_values():
    """Should map every Outcome member to a word, no two outcomes sharing one."""
    assert set(_WORDS) == set(Outcome)
    assert len(set(_WORDS.values())) == len(Outcome)


# ---------------------------------------------------------------------------
# _ALERT_TYPES — every Outcome maps to the token its site passes today
# ---------------------------------------------------------------------------


def test_alert_types_covers_every_outcome_with_the_pinned_token():
    """Should map every Outcome member to exactly the alert token that outcome's call
    site passed before composition moved into notify.py, so a telegram_alerts list
    written before this change keeps selecting the same alerts."""
    assert _ALERT_TYPES == {
        Outcome.TASK_DONE: "task",
        Outcome.RUN_DONE: "done",
        Outcome.MANUAL_STOP: "stop",
        Outcome.UNCONVERGED: "task-fail",
        Outcome.HALTED: "stop",
        Outcome.ESCALATED: "escalation",
        Outcome.ERRORED: "stop",
        Outcome.FORCE_QUIT: "stop",
    }


# ---------------------------------------------------------------------------
# report() — passes _ALERT_TYPES[outcome] through to notify()
# ---------------------------------------------------------------------------


def test_report_passes_the_pinned_alert_type_to_notify_for_every_outcome(monkeypatch):
    """Should call notify() with _ALERT_TYPES[outcome] for every Outcome member, so the
    word and the colour cannot come apart."""
    recorded = []
    monkeypatch.setattr(notify_module, "notify", lambda config, text, alert_type: recorded.append(alert_type))
    config = _config(["task", "done", "stop", "task-fail", "escalation"])

    for outcome in Outcome:
        report(config, outcome, "myproject", None, "Ran for 1m 0s · 2 tasks done")

    assert recorded == [_ALERT_TYPES[outcome] for outcome in Outcome]

"""Unit tests for _run_summary formatting, _fmt_elapsed, _with_caffeinate,
and _handle_sigint."""

import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from orchestrator import state
from orchestrator.notify import Outcome
from orchestrator.runtime import _fmt_elapsed, _handle_sigint, _run_summary, _with_caffeinate
import orchestrator.runtime as runtime


def test_run_summary_with_elapsed_and_count():
    """Should append the ' · N tasks done' suffix when run_started is set."""
    saved_started, saved_done = state.run_started, state.tasks_done
    try:
        state.run_started = time.monotonic() - 5
        state.tasks_done = 3
        result = _run_summary()
        assert result.startswith("Ran for ")
        assert result.endswith(" · 3 tasks done")
    finally:
        state.run_started, state.tasks_done = saved_started, saved_done


def test_run_summary_with_no_run_started():
    """Should read exactly 'Ran for unknown · 0 tasks done' when run_started is None."""
    saved_started, saved_done = state.run_started, state.tasks_done
    try:
        state.run_started = None
        state.tasks_done = 0
        assert _run_summary() == "Ran for unknown · 0 tasks done"
    finally:
        state.run_started, state.tasks_done = saved_started, saved_done


# ---------------------------------------------------------------------------
# Task 1: _fmt_elapsed — format branches and hour boundary
# ---------------------------------------------------------------------------


def test_fmt_elapsed_under_one_hour():
    """Should return 'Xm Ys' format when seconds is under one hour (no-hours branch)."""
    assert _fmt_elapsed(125) == "2m 5s"


def test_fmt_elapsed_one_hour_or_more():
    """Should return 'Xh Ym Zs' format when seconds is one hour or more."""
    assert _fmt_elapsed(3661) == "1h 1m 1s"


def test_fmt_elapsed_zero_seconds():
    """Should return '0m 0s' when seconds is 0 (hours branch not taken)."""
    assert _fmt_elapsed(0) == "0m 0s"


def test_fmt_elapsed_lower_boundary_3599():
    """Should format '59m 59s' without hours at the 3599 lower boundary."""
    assert _fmt_elapsed(3599) == "59m 59s"


def test_fmt_elapsed_exact_hour_boundary_3600():
    """Should format '1h 0m 0s' at the exact 3600 hour boundary."""
    assert _fmt_elapsed(3600) == "1h 0m 0s"


# ---------------------------------------------------------------------------
# Task 2: _with_caffeinate — success paths (caffeinate unavailable vs available)
# ---------------------------------------------------------------------------

_ELAPSED_RE = re.compile(r"^(\d+h )?\d+m \d+s$")


def test_with_caffeinate_unavailable_runs_func_directly(monkeypatch):
    """Should run func directly and return formatted elapsed when caffeinate is unavailable
    (FileNotFoundError branch) — the non-macOS degrade path."""
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=FileNotFoundError))
    func = Mock()

    result = _with_caffeinate(func)

    func.assert_called_once()
    assert _ELAPSED_RE.match(result)


def test_with_caffeinate_available_wraps_and_cleans_up_on_success(monkeypatch):
    """Should wrap func with a caffeinate Popen + SIGTERM cleanup and return formatted
    elapsed when caffeinate is available and func succeeds."""
    fake_proc = Mock(spec=["send_signal", "wait"])
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(return_value=fake_proc))
    func = Mock()

    result = _with_caffeinate(func)

    func.assert_called_once()
    fake_proc.send_signal.assert_called_once_with(signal.SIGTERM)
    fake_proc.wait.assert_called_once()
    assert _ELAPSED_RE.match(result)


# ---------------------------------------------------------------------------
# Task 3: _with_caffeinate — exception propagation on both branches and
# cleanup on failure
# ---------------------------------------------------------------------------


def test_with_caffeinate_unavailable_reraises_and_prints(monkeypatch, capsys):
    """Should re-emit 'Ran for ... before stopping.' and re-raise when func raises
    and caffeinate is unavailable."""
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=FileNotFoundError))
    func = Mock(side_effect=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        _with_caffeinate(func)

    out = capsys.readouterr().out
    assert "Ran for" in out
    assert "before stopping." in out


def test_with_caffeinate_available_reraises_and_prints(monkeypatch, capsys):
    """Should re-emit 'Ran for ... before stopping.' and re-raise when func raises
    and caffeinate is available (independent code path from the FileNotFoundError branch)."""
    fake_proc = Mock(spec=["send_signal", "wait"])
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(return_value=fake_proc))
    func = Mock(side_effect=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        _with_caffeinate(func)

    out = capsys.readouterr().out
    assert "Ran for" in out
    assert "before stopping." in out


def test_with_caffeinate_available_cleans_up_on_exception(monkeypatch, capsys):
    """Should still call caffeinate.send_signal(SIGTERM) and wait() (finally cleanup)
    when func raises and caffeinate is available."""
    fake_proc = Mock(spec=["send_signal", "wait"])
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(return_value=fake_proc))
    func = Mock(side_effect=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        _with_caffeinate(func)

    fake_proc.send_signal.assert_called_once_with(signal.SIGTERM)
    fake_proc.wait.assert_called_once()


# ---------------------------------------------------------------------------
# _with_caffeinate — sleep prevention survives a soft stop
# ---------------------------------------------------------------------------

_SIGINT_SCRIPT = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    from orchestrator import runtime

    signal.signal(signal.SIGINT, runtime._handle_sigint)

    real_popen = subprocess.Popen
    spawned = []

    def fake_popen(args, **kwargs):
        stand_in = [sys.executable, "-c", "import time; time.sleep(60)", *args[1:]]
        proc = real_popen(stand_in, **kwargs)
        spawned.append(proc)
        return proc

    runtime.subprocess.Popen = fake_popen
    outcome = []

    def func():
        time.sleep(0.5)
        os.killpg(os.getpgrp(), signal.SIGINT)
        try:
            spawned[0].wait(timeout=2)
        except subprocess.TimeoutExpired:
            outcome.append("alive")
        else:
            outcome.append("died")

    runtime._with_caffeinate(func)

    if outcome != ["alive"]:
        print("sleep prevention died on SIGINT to the run's group", file=sys.stderr)
        sys.exit(1)
    """
)


def test_with_caffeinate_survives_sigint_to_run_group():
    """A SIGINT delivered to the run's own process group while the wrapped function
    runs leaves the sleep-prevention process alive."""
    repo_root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [sys.executable, "-c", _SIGINT_SCRIPT],
        cwd=repo_root,
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Task 4: _handle_sigint — first Ctrl+C sets stop_requested without exiting
# ---------------------------------------------------------------------------


def test_handle_sigint_first_press_sets_stop_requested(capsys):
    """Should set state.stop_requested to True and print a warning without exiting
    on first Ctrl+C."""
    saved_stop = state.stop_requested
    try:
        state.stop_requested = False
        _handle_sigint(signal.SIGINT, None)
        assert state.stop_requested is True
        out = capsys.readouterr().out
        assert "Will stop after the current task finishes." in out
    finally:
        state.stop_requested = saved_stop


# ---------------------------------------------------------------------------
# _handle_sigint — second Ctrl+C force-quits, with and without the
# report guard
# ---------------------------------------------------------------------------


def test_handle_sigint_second_press_reports_when_config_and_project_dir_set(monkeypatch):
    """Should call kill_active_child, send a force-quit report, and sys.exit(1) on
    second Ctrl+C when state.config and state.project_dir are both set."""
    saved_stop, saved_config, saved_dir = state.stop_requested, state.config, state.project_dir
    kill_mock = Mock()
    report_mock = Mock()
    monkeypatch.setattr(runtime, "kill_active_child", kill_mock)
    monkeypatch.setattr(runtime, "report", report_mock)
    try:
        state.stop_requested = True
        state.config = Mock()
        state.project_dir = Mock(name="project_dir")
        state.project_dir.name = "myproject"

        with pytest.raises(SystemExit) as exc_info:
            _handle_sigint(signal.SIGINT, None)

        assert exc_info.value.code == 1
        kill_mock.assert_called_once()
        report_mock.assert_called_once()
        call_args = report_mock.call_args
        assert call_args[0][0] is state.config
        assert call_args[0][1] is Outcome.FORCE_QUIT
        assert call_args[0][2] == "myproject"
    finally:
        state.stop_requested, state.config, state.project_dir = saved_stop, saved_config, saved_dir


def test_handle_sigint_second_press_no_report_when_config_none(monkeypatch):
    """Should call kill_active_child and sys.exit(1) WITHOUT reporting when
    state.config is None."""
    saved_stop, saved_config, saved_dir = state.stop_requested, state.config, state.project_dir
    kill_mock = Mock()
    report_mock = Mock()
    monkeypatch.setattr(runtime, "kill_active_child", kill_mock)
    monkeypatch.setattr(runtime, "report", report_mock)
    try:
        state.stop_requested = True
        state.config = None
        state.project_dir = Mock()

        with pytest.raises(SystemExit) as exc_info:
            _handle_sigint(signal.SIGINT, None)

        assert exc_info.value.code == 1
        kill_mock.assert_called_once()
        report_mock.assert_not_called()
    finally:
        state.stop_requested, state.config, state.project_dir = saved_stop, saved_config, saved_dir


def test_handle_sigint_second_press_no_report_when_project_dir_none(monkeypatch):
    """Should call kill_active_child and sys.exit(1) WITHOUT reporting when
    state.project_dir is None."""
    saved_stop, saved_config, saved_dir = state.stop_requested, state.config, state.project_dir
    kill_mock = Mock()
    report_mock = Mock()
    monkeypatch.setattr(runtime, "kill_active_child", kill_mock)
    monkeypatch.setattr(runtime, "report", report_mock)
    try:
        state.stop_requested = True
        state.config = Mock()
        state.project_dir = None

        with pytest.raises(SystemExit) as exc_info:
            _handle_sigint(signal.SIGINT, None)

        assert exc_info.value.code == 1
        kill_mock.assert_called_once()
        report_mock.assert_not_called()
    finally:
        state.stop_requested, state.config, state.project_dir = saved_stop, saved_config, saved_dir

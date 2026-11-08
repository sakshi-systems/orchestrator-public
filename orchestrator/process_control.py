"""Shared child-process cleanup — killing the active agent child process group on interruption."""

from __future__ import annotations

import os
import signal

from . import state


def kill_active_child() -> None:
    """Kill the in-flight agent child process group, if any."""
    proc = state.active_proc
    if proc is None or proc.poll() is not None:
        state.active_proc = None
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass
    state.active_proc = None

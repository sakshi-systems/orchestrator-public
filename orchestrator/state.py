"""Shared mutable state for the orchestrator process."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .config import OrchestratorConfig

stop_requested: bool = False
queue_exhausted: bool = False
active_proc: subprocess.Popen | None = None
run_started: float | None = None
tasks_done: int = 0
config: "OrchestratorConfig | None" = None
project_dir: "Path | None" = None

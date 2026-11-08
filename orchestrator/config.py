"""Orchestrator configuration — loaded from orchestrator.json in the project root at startup."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OrchestratorConfig:
    max_iterations: int
    usage_threshold_5h: float
    usage_threshold_weekly: float
    enable_phase_sessions: bool
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_alerts: list[str] = field(default_factory=list)
    roadmap_path: str | None = None
    runtime: str = "claude"


AGENT_RUNTIMES = ("claude", "codex")


def load_config(project_dir: Path | None = None) -> OrchestratorConfig:
    """Load and validate config from orchestrator.json in the project root (or ORCHESTRATOR_CONFIG override).

    When `project_dir` is given and `<project_dir>/.ai-factory/orchestrator.json` exists, its keys are
    shallow-merged onto the base config (project keys win, absent keys inherit the base value). Absence of
    the override leaves the result byte-identical to calling this with no argument.
    """
    default = Path(__file__).parent.parent / "orchestrator.json"
    path = Path(os.environ.get("ORCHESTRATOR_CONFIG", str(default)))

    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}\n"
            f"Create it with all required fields:\n"
            f'{{"max_iterations": 3, "usage_threshold_5h": 90, "usage_threshold_weekly": 95, "enable_phase_sessions": true}}'
        )

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"Config file is not valid JSON: {path}\n{e}")

    required = ["max_iterations", "usage_threshold_5h", "usage_threshold_weekly", "enable_phase_sessions"]
    for key in required:
        if key not in data:
            raise SystemExit(f"Missing required key '{key}' in {path}")

    if project_dir is not None:
        override_path = project_dir / ".ai-factory" / "orchestrator.json"
        if override_path.exists():
            try:
                override = json.loads(override_path.read_text())
            except json.JSONDecodeError as e:
                raise SystemExit(f"Config file is not valid JSON: {override_path}\n{e}")
            if not isinstance(override, dict):
                raise SystemExit(f"Config file is not valid JSON: {override_path}\nExpected a JSON object, got {type(override).__name__}")
            data.update(override)

    roadmap_path = data.get("roadmap_path") or None
    if roadmap_path is not None and (Path(roadmap_path).is_absolute() or ".." in Path(roadmap_path).parts):
        raise SystemExit(f"Invalid 'roadmap_path' in {path}: {roadmap_path!r} (must be a relative path with no '..' segments)")

    runtime = data["runtime"] if "runtime" in data else "claude"
    if not isinstance(runtime, str) or runtime not in AGENT_RUNTIMES:
        raise SystemExit(f"Invalid 'runtime' in {path}: {runtime!r} (must be 'claude' or 'codex')")

    return OrchestratorConfig(
        max_iterations=int(data["max_iterations"]),
        usage_threshold_5h=float(data["usage_threshold_5h"]),
        usage_threshold_weekly=float(data["usage_threshold_weekly"]),
        enable_phase_sessions=bool(data["enable_phase_sessions"]),
        telegram_bot_token=data.get("telegram_bot_token") or None,
        telegram_chat_id=data.get("telegram_chat_id") or None,
        telegram_alerts=data.get("telegram_alerts") or [],
        roadmap_path=roadmap_path,
        runtime=runtime,
    )

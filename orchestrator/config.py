"""Orchestrator configuration — loaded from orchestrator.json in the project root at startup."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .ports import AgentRole


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
    claude_agents: dict[str, dict[str, str]] = field(default_factory=dict)
    codex_agents: dict[str, dict[str, str]] = field(default_factory=dict)
    plan_review_iterations: int | None = None
    task_reviewer: str = "run"


AGENT_RUNTIMES = ("claude", "codex")


def _validate_agent_block(key: str, block: object, path: Path) -> dict[str, dict[str, str]]:
    """Validate an effective runtime block without adding defaults or changing values."""
    if not isinstance(block, dict):
        raise SystemExit(f"Invalid '{key}' in {path}: {block!r} (must be an object)")

    roles = {role.value for role in AgentRole}
    for role, settings in block.items():
        if role not in roles:
            raise SystemExit(f"Invalid '{key}' role {role!r} in {path}: {settings!r} (unknown role)")
        if not isinstance(settings, dict):
            raise SystemExit(f"Invalid '{key}' role {role!r} in {path}: {settings!r} (must be an object)")
        for name, value in settings.items():
            if name not in ("model", "effort"):
                raise SystemExit(
                    f"Invalid '{key}' role {role!r} field {name!r} in {path}: {value!r} (unknown field)"
                )
            if not isinstance(value, str) or not value:
                raise SystemExit(
                    f"Invalid '{key}' role {role!r} field {name!r} in {path}: {value!r} (must be a non-empty string)"
                )
    return block


def load_config(project_dir: Path | None = None, *, task_reviewers: Mapping[str, object]) -> OrchestratorConfig:
    """Load and validate config from orchestrator.json in the project root (or ORCHESTRATOR_CONFIG override).

    When `project_dir` is given and `<project_dir>/.ai-factory/orchestrator.json` exists, its keys are
    shallow-merged onto the base config (project keys win, absent keys inherit the base value). Absence of
    the override leaves the result byte-identical to calling this with no argument. `task_reviewers` names
    the accepted task-reviewer spellings; the names are handed in by the caller that owns the kinds.
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

    override = {}
    override_path = path
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

    def _supplying(key: str) -> Path:
        """Return the path of the file that supplies `key` after the merge."""
        return override_path if key in override else path

    roadmap_path = data.get("roadmap_path") or None
    if roadmap_path is not None and (Path(roadmap_path).is_absolute() or ".." in Path(roadmap_path).parts):
        raise SystemExit(f"Invalid 'roadmap_path' in {_supplying('roadmap_path')}: {roadmap_path!r} (must be a relative path with no '..' segments)")

    runtime = data["runtime"] if "runtime" in data else "claude"
    if not isinstance(runtime, str) or runtime not in AGENT_RUNTIMES:
        raise SystemExit(f"Invalid 'runtime' in {_supplying('runtime')}: {runtime!r} (must be 'claude' or 'codex')")

    plan_review_iterations = None
    if "plan_review_iterations" in data:
        value = data["plan_review_iterations"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SystemExit(
                f"Invalid 'plan_review_iterations' in {_supplying('plan_review_iterations')}: "
                f"{value!r} (must be a non-negative integer)"
            )
        plan_review_iterations = value

    task_reviewer = data.get("task_reviewer", "run")
    if not isinstance(task_reviewer, str) or task_reviewer not in task_reviewers:
        raise SystemExit(
            f"Invalid 'task_reviewer' in {_supplying('task_reviewer')}: "
            f"{task_reviewer!r} (must be {' or '.join(repr(name) for name in task_reviewers)})"
        )

    agent_blocks = {}
    for key in ("claude_agents", "codex_agents"):
        agent_blocks[key] = _validate_agent_block(key, data.get(key, {}), _supplying(key))

    max_iterations = int(data["max_iterations"])
    if max_iterations < 1:
        raise SystemExit(
            f"Invalid 'max_iterations' in {_supplying('max_iterations')}: "
            f"{max_iterations!r} (must be a positive integer)"
        )

    return OrchestratorConfig(
        max_iterations=max_iterations,
        usage_threshold_5h=float(data["usage_threshold_5h"]),
        usage_threshold_weekly=float(data["usage_threshold_weekly"]),
        enable_phase_sessions=bool(data["enable_phase_sessions"]),
        telegram_bot_token=data.get("telegram_bot_token") or None,
        telegram_chat_id=data.get("telegram_chat_id") or None,
        telegram_alerts=data.get("telegram_alerts") or [],
        roadmap_path=roadmap_path,
        runtime=runtime,
        claude_agents=agent_blocks["claude_agents"],
        codex_agents=agent_blocks["codex_agents"],
        plan_review_iterations=plan_review_iterations,
        task_reviewer=task_reviewer,
    )

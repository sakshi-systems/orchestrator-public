"""Orchestrator configuration — loaded from orchestrator.json in the project root at startup."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .locator import is_path_text, is_single_component
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
    roles: dict[str, dict[str, str]] = field(default_factory=dict)
    hermes_instances: dict[str, dict[str, object]] = field(default_factory=dict)


AGENT_RUNTIMES = ("claude", "codex")


def role_runtimes(runtime: str, roles: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, dict[str, str]]:
    """The effective runtime of each of the run's four roles, as a record keyed by role.

    A role the `roles` object names takes that runtime; every role it leaves out
    takes the run's `runtime`. Each entry names the role's runtime — the shape
    the sidecar's `runtime` record holds.
    """
    block = roles if roles is not None else {}
    return {
        role.value: dict(block.get(role.value, {"runtime": runtime}))
        for role in AgentRole
    }


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


def _validate_roles(block: object, path: Path, hermes_instances: Mapping[str, object]) -> dict[str, dict[str, str]]:
    """Validate the effective `roles` object without adding defaults or changing values."""
    if not isinstance(block, dict):
        raise SystemExit(f"Invalid 'roles' in {path}: {block!r} (must be an object)")

    roles = {role.value for role in AgentRole}
    for role, value in block.items():
        if role not in roles:
            raise SystemExit(f"Invalid 'roles' role {role!r} in {path}: {value!r} (unknown role)")
        if not isinstance(value, dict):
            raise SystemExit(f"Invalid 'roles' role {role!r} in {path}: {value!r} (must be an object)")
        for name, field_value in value.items():
            runtime = value.get("runtime")
            if runtime == "hermes":
                allowed = {"runtime", "instance"}
            elif runtime == "head":
                allowed = {"runtime", "head"}
            else:
                allowed = {"runtime"}
            if name not in allowed:
                raise SystemExit(
                    f"Invalid 'roles' role {role!r} field {name!r} in {path}: {field_value!r} (unknown field)"
                )
        runtime = value.get("runtime")
        if not isinstance(runtime, str) or not runtime:
            raise SystemExit(
                f"Invalid 'roles' role {role!r} field 'runtime' in {path}: {runtime!r} (must be a non-empty string)"
            )
        if runtime == "head":
            if role == "implementer":
                raise SystemExit(f"Invalid 'roles' role 'implementer' in {path}: a head cannot take the implementer")
            head = value.get("head")
            if not isinstance(head, dict) or set(head) != {"repository", "owner", "folder"}:
                raise SystemExit(
                    f"Invalid 'roles' role {role!r} field 'head' in {path}: {head!r} "
                    "(must hold exactly 'repository', 'owner' and 'folder')"
                )
            for field in ("repository", "owner", "folder"):
                if not isinstance(head[field], str) or not head[field]:
                    raise SystemExit(
                        f"Invalid 'roles' role {role!r} field 'head' in {path}: "
                        f"{field} must be a nonempty string"
                    )
            if not Path(head["repository"]).is_absolute():
                raise SystemExit(
                    f"Invalid 'roles' role {role!r} field 'head' in {path}: "
                    f"repository must be an absolute path"
                )
            if not is_path_text(head["repository"]):
                raise SystemExit(
                    f"Invalid 'roles' role {role!r} field 'head' in {path}: "
                    f"repository must be a path the filesystem can name"
                )
            for field in ("owner", "folder"):
                if not is_single_component(head[field]):
                    raise SystemExit(
                        f"Invalid 'roles' role {role!r} field 'head' in {path}: "
                        f"{field} must be a single path component"
                    )
            continue
        if runtime == "hermes":
            instance = value.get("instance")
            if not isinstance(instance, str) or not instance:
                raise SystemExit(
                    f"Invalid 'roles' role {role!r} field 'instance' in {path}: {instance!r} (must be a non-empty string)"
                )
            if instance not in hermes_instances:
                raise SystemExit(
                    f"Invalid 'roles' role {role!r} in {path}: {instance!r} (not a name 'hermes_instances' holds)"
                )
            continue
        if runtime not in AGENT_RUNTIMES:
            raise SystemExit(
                f"Invalid 'roles' role {role!r} field 'runtime' in {path}: {runtime!r} (must be 'claude' or 'codex')"
            )
    return block


_HERMES_INSTANCE_FIELDS = ("server", "port", "key_file")


def _validate_hermes_instances(block: object, path: Path) -> dict[str, dict[str, object]]:
    """Validate the machine's instance map; the key material is never read here."""
    if not isinstance(block, dict):
        raise SystemExit(f"Invalid 'hermes_instances' in {path}: {block!r} (must be an object)")
    for name, entry in block.items():
        if not isinstance(name, str) or not name:
            raise SystemExit(
                f"Invalid 'hermes_instances' name {name!r} in {path}: {entry!r} (must be a non-empty string)"
            )
        if not isinstance(entry, dict) or set(entry) != set(_HERMES_INSTANCE_FIELDS):
            raise SystemExit(
                f"Invalid 'hermes_instances' instance {name!r} in {path}: {entry!r} "
                "(must hold exactly 'server', 'port' and 'key_file')"
            )
        for field in ("server", "key_file"):
            if not isinstance(entry[field], str) or not entry[field]:
                raise SystemExit(
                    f"Invalid 'hermes_instances' instance {name!r} field {field!r} in {path}: "
                    f"{entry[field]!r} (must be a non-empty string)"
                )
        port = entry["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise SystemExit(
                f"Invalid 'hermes_instances' instance {name!r} field 'port' in {path}: "
                f"{port!r} (must be an integer from 1 to 65535)"
            )
    return block


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

    if "hermes_instances" in override:
        raise SystemExit(
            f"Invalid 'hermes_instances' in {override_path}: {override['hermes_instances']!r} "
            "(the base file supplies the instance map, never a project overlay)"
        )
    hermes_instances = _validate_hermes_instances(data.get("hermes_instances", {}), path)
    roles = _validate_roles(data.get("roles", {}), _supplying("roles"), hermes_instances)

    plan_review_iterations = None
    if "plan_review_iterations" in data:
        value = data["plan_review_iterations"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SystemExit(
                f"Invalid 'plan_review_iterations' in {_supplying('plan_review_iterations')}: "
                f"{value!r} (must be a non-negative integer)"
            )
        plan_review_iterations = value

    if "task_reviewer" in data:
        raise SystemExit(
            f"Invalid 'task_reviewer' in {_supplying('task_reviewer')}: "
            f"the setting is retired; name who reviews a task as the runtime of 'roles.code_reviewer'"
        )

    agent_blocks = {}
    for key in ("claude_agents", "codex_agents"):
        agent_blocks[key] = _validate_agent_block(key, data.get(key, {}), _supplying(key))

    max_iterations = data["max_iterations"]
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or max_iterations < 1:
        raise SystemExit(
            f"Invalid 'max_iterations' in {_supplying('max_iterations')}: "
            f"{max_iterations!r} (must be a positive integer)"
        )

    for key in ("usage_threshold_5h", "usage_threshold_weekly"):
        threshold = data[key]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
            raise SystemExit(
                f"Invalid '{key}' in {_supplying(key)}: "
                f"{threshold!r} (must be a finite number)"
            )

    enable_phase_sessions = data["enable_phase_sessions"]
    if not isinstance(enable_phase_sessions, bool):
        raise SystemExit(
            f"Invalid 'enable_phase_sessions' in {_supplying('enable_phase_sessions')}: "
            f"{enable_phase_sessions!r} (must be a boolean)"
        )

    return OrchestratorConfig(
        max_iterations=max_iterations,
        usage_threshold_5h=float(data["usage_threshold_5h"]),
        usage_threshold_weekly=float(data["usage_threshold_weekly"]),
        enable_phase_sessions=enable_phase_sessions,
        roles=roles,
        hermes_instances=hermes_instances,
        telegram_bot_token=data.get("telegram_bot_token") or None,
        telegram_chat_id=data.get("telegram_chat_id") or None,
        telegram_alerts=data.get("telegram_alerts") or [],
        roadmap_path=roadmap_path,
        runtime=runtime,
        claude_agents=agent_blocks["claude_agents"],
        codex_agents=agent_blocks["codex_agents"],
        plan_review_iterations=plan_review_iterations,
    )

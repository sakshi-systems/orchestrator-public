"""Orchestrator configuration — the defaults, the machine's base and a person's
run settings layered at startup."""

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
    max_iterations: int = 3
    usage_threshold_5h: float = 90.0
    usage_threshold_weekly: float = 95.0
    enable_phase_sessions: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_alerts: list[str] = field(default_factory=list)
    roadmap_path: str | None = None
    runtime: str = "claude"
    claude_agents: dict[str, dict[str, str]] = field(default_factory=dict)
    codex_agents: dict[str, dict[str, str]] = field(default_factory=dict)
    plan_review_iterations: int | None = None
    planning: bool = True
    roles: dict[str, dict[str, str]] = field(default_factory=dict)
    hermes_instances: dict[str, dict[str, object]] = field(default_factory=dict)


AGENT_RUNTIMES = ("claude", "codex")

# The keys a person's run settings file may hold — every value that shapes a
# run. Every other key found in the file is refused, so the file is the one
# home of these values and holds nothing else; the base refuses each of them,
# for none of them is the machine's to set.
RUN_KEYS = (
    "max_iterations",
    "plan_review_iterations",
    "planning",
    "usage_threshold_5h",
    "usage_threshold_weekly",
    "enable_phase_sessions",
    "roadmap_path",
    "runtime",
    "roles",
    "claude_agents",
    "codex_agents",
    "telegram_alerts",
)

# What the machine's base holds, and all it holds: the instances a role may
# name and the notification credentials. Every key found in it that is neither
# one of these nor a run key above is ignored.
_BASE_KEYS = ("hermes_instances", "telegram_bot_token", "telegram_chat_id")

_ALL_ROLES = tuple(AgentRole)


def role_runtimes(
    runtime: str,
    roles: Mapping[str, Mapping[str, str]] | None = None,
    wanted: tuple[AgentRole, ...] = _ALL_ROLES,
) -> dict[str, dict[str, str]]:
    """The effective runtime of each role the run has, as a record keyed by role.

    The record holds the roles `wanted` — the roles the run has. A role the
    `roles` object names takes that runtime; every role it leaves out takes
    the run's `runtime`. Each entry names the role's runtime — the shape the
    sidecar's `runtime` record holds.
    """
    block = roles if roles is not None else {}
    return {
        role.value: dict(block.get(role.value, {"runtime": runtime}))
        for role in wanted
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


def _run_settings_file(project_dir: Path | None, user_slug: str | None) -> Path:
    """The person's run settings file, as the startup messages name it.

    `<slug>` stands in where the run knows no slug, and without a project
    directory the path is named in its project-relative form.
    """
    slug = user_slug if user_slug else "<slug>"
    directory = Path(".ai-factory") / "run-settings"
    if project_dir is not None:
        directory = project_dir / directory
    return directory / f"{slug}.json"


def load_config(project_dir: Path | None = None, user_slug: str | None = None) -> OrchestratorConfig:
    """Load and validate the configuration from the machine's base and the person's
    run settings.

    The base is `~/.local/state/orchestrator/orchestrator.json`, resolved from `Path.home()`
    at call time, unless `ORCHESTRATOR_CONFIG` names another path. A path the environment
    variable names must hold a file — a missing one exits naming it — while the default
    path's absence means no base and every key takes its default. A base that is not valid
    JSON, or not a JSON object, exits naming its path. The base holds `hermes_instances`
    and the Telegram credentials and nothing else: a key of `RUN_KEYS` found in it exits
    naming the key, the base and the person's file the key belongs in, and every other key
    it carries that is not one of the machine's own is ignored.

    When `project_dir` and `user_slug` are given, the person's run settings
    `<project_dir>/.ai-factory/run-settings/<user_slug>.json` layer shallowly onto the
    base (a key of the person's file replaces the value below it wholesale; a run key
    it omits takes its default, and a missing file loads the base and the defaults
    alone). The file refuses any key outside `RUN_KEYS`, `hermes_instances` and the
    Telegram credentials, naming itself and the key; a legacy
    `<project_dir>/.ai-factory/orchestrator.json` that exists exits naming itself and
    the person's file, and is never read.
    """
    if project_dir is not None:
        legacy_path = project_dir / ".ai-factory" / "orchestrator.json"
        if legacy_path.exists():
            raise SystemExit(
                f"Config file is no longer read: {legacy_path}\n"
                f"Move its values into {_run_settings_file(project_dir, user_slug)} and delete it."
            )

    default = Path.home() / ".local" / "state" / "orchestrator" / "orchestrator.json"
    if "ORCHESTRATOR_CONFIG" in os.environ:
        path = Path(os.environ["ORCHESTRATOR_CONFIG"])
        if not path.exists():
            raise SystemExit(f"Config file not found: {path}")
    else:
        path = default

    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise SystemExit(f"Config file is not valid JSON: {path}\n{e}")
        if not isinstance(data, dict):
            raise SystemExit(f"Config file is not valid JSON: {path}\nExpected a JSON object, got {type(data).__name__}")
    else:
        data = {}

    # The base holds the machine's keys alone. A key that shapes a run found
    # in it is a startup error naming the person's file the key belongs in,
    # and every key that is neither a machine key nor a run key is ignored —
    # so no value that shapes a run can come from the base.
    for key in data:
        if key in RUN_KEYS:
            raise SystemExit(
                f"Invalid '{key}' in {path} "
                f"(a value that shapes a run; it belongs in {_run_settings_file(project_dir, user_slug)})"
            )
    data = {key: data[key] for key in _BASE_KEYS if key in data}

    person = {}
    person_path = path
    if project_dir is not None and user_slug:
        person_path = project_dir / ".ai-factory" / "run-settings" / f"{user_slug}.json"
        if person_path.exists():
            try:
                person = json.loads(person_path.read_text())
            except json.JSONDecodeError as e:
                raise SystemExit(f"Config file is not valid JSON: {person_path}\n{e}")
            if not isinstance(person, dict):
                raise SystemExit(f"Config file is not valid JSON: {person_path}\nExpected a JSON object, got {type(person).__name__}")

    if "task_reviewer" in person:
        raise SystemExit(
            f"Invalid 'task_reviewer' in {person_path}: "
            "the setting is retired; name who reviews a task as the runtime of 'roles.code_reviewer'"
        )
    # Every refusal in this loop names the file and the key alone — never the
    # value, for a misspelled credential key lands here too and its secret must
    # not print.
    for key in person:
        if key == "hermes_instances":
            raise SystemExit(
                f"Invalid 'hermes_instances' in {person_path} "
                "(the base file alone holds the instance map)"
            )
        if key in ("telegram_bot_token", "telegram_chat_id"):
            raise SystemExit(
                f"Invalid '{key}' in {person_path} "
                "(the machine's credentials, which the base file holds)"
            )
        if key not in RUN_KEYS:
            raise SystemExit(f"Invalid '{key}' in {person_path} (unknown key)")
    data.update(person)

    # A value that reaches validation below can only come from the person's
    # file — the base holds no run key and a default always validates — so
    # every diagnostic names the person's file.
    roadmap_path = data.get("roadmap_path") or None
    if roadmap_path is not None and (Path(roadmap_path).is_absolute() or ".." in Path(roadmap_path).parts):
        raise SystemExit(f"Invalid 'roadmap_path' in {person_path}: {roadmap_path!r} (must be a relative path with no '..' segments)")

    runtime = data["runtime"] if "runtime" in data else "claude"
    if not isinstance(runtime, str) or runtime not in AGENT_RUNTIMES:
        raise SystemExit(f"Invalid 'runtime' in {person_path}: {runtime!r} (must be 'claude' or 'codex')")

    hermes_instances = _validate_hermes_instances(data.get("hermes_instances", {}), path)
    roles = _validate_roles(data.get("roles", {}), person_path, hermes_instances)

    plan_review_iterations = None
    if "plan_review_iterations" in data:
        value = data["plan_review_iterations"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SystemExit(
                f"Invalid 'plan_review_iterations' in {person_path}: "
                f"{value!r} (must be a non-negative integer)"
            )
        plan_review_iterations = value

    planning = True
    if "planning" in data:
        planning = data["planning"]
        if not isinstance(planning, bool):
            raise SystemExit(
                f"Invalid 'planning' in {person_path}: "
                f"{planning!r} (must be a boolean)"
            )

    agent_blocks = {}
    for key in ("claude_agents", "codex_agents"):
        agent_blocks[key] = _validate_agent_block(key, data.get(key, {}), person_path)

    max_iterations = data.get("max_iterations", OrchestratorConfig.max_iterations)
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or max_iterations < 1:
        raise SystemExit(
            f"Invalid 'max_iterations' in {person_path}: "
            f"{max_iterations!r} (must be a positive integer)"
        )

    thresholds = {}
    for key in ("usage_threshold_5h", "usage_threshold_weekly"):
        threshold = data.get(key, getattr(OrchestratorConfig, key))
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
            raise SystemExit(
                f"Invalid '{key}' in {person_path}: "
                f"{threshold!r} (must be a finite number)"
            )
        thresholds[key] = float(threshold)

    enable_phase_sessions = data.get("enable_phase_sessions", OrchestratorConfig.enable_phase_sessions)
    if not isinstance(enable_phase_sessions, bool):
        raise SystemExit(
            f"Invalid 'enable_phase_sessions' in {person_path}: "
            f"{enable_phase_sessions!r} (must be a boolean)"
        )

    return OrchestratorConfig(
        max_iterations=max_iterations,
        usage_threshold_5h=thresholds["usage_threshold_5h"],
        usage_threshold_weekly=thresholds["usage_threshold_weekly"],
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
        planning=planning,
    )

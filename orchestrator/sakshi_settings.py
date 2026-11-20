"""Sakshi's settings: one validated file, never converted, refused by
key with the file named.

Standard library only, beside ``reviews``, whose repository and slug
forms these settings share. Nothing of the run, the flow or Sakshi's
root is imported, and nothing imports this yet.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from .reviews import (USER_NAME, is_repository, is_slug, is_user_name,
                      same_repository)

SETTINGS_NAME = "sakshi.json"

_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_ENVIRONMENT_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
# the parent session's messaging and identity, which a role never
# inherits even when the settings list them
_REFUSED_ENVIRONMENT_NAMES = ("CLAUDE_CODE_MESSAGING_SOCKET",
                              "CLAUDE_CODE_MESSAGING_TOKEN",
                              "CLAUDE_CODE_SESSION_ID")
_TOP_LEVEL = ("workspace_root", "environment", "telegram_bot_token",
              "telegram_chat_id", "telegram_alerts", "repositories")
_REPOSITORY_KEYS = ("checkout", "team")
_TEAM_KEYS = ("memory", "deadline_seconds", "roles", "musafir")
_TEAM_REQUIRED = ("memory", "deadline_seconds")
_ROLE_KEYS = ("seed", "user", "model", "effort", "head")
_ROLE_OPTIONAL = ("source",)


@dataclass(frozen=True)
class RoleSettings:
    """One role or the musafir: where it starts, as whom, how it runs,
    and the head it lives under."""

    seed: Path
    user: str
    model: str
    effort: str
    head: str
    # where Sakshi clones the seed from, when it gives one
    source: str | None = None

    def seed_identity(self) -> Path:
        """The one identity of the seed: the directory its spelling
        resolves to, so two spellings of one directory are one seed."""
        return self.seed.resolve()


@dataclass(frozen=True)
class ReviewTeam:
    """The one team that reviews a repository. No roles or no musafir
    loads; a request for the repository is refused at request time."""

    memory: Path
    deadline_seconds: int | float
    roles: dict[str, RoleSettings] = field(default_factory=dict)
    musafir: RoleSettings | None = None


@dataclass(frozen=True)
class ReviewedRepository:
    """One reviewed repository: its checkout and its team."""

    checkout: Path
    team: ReviewTeam


@dataclass(frozen=True)
class SakshiSettings:
    """Everything Sakshi reads from its settings file. An absent file
    is no repository, a valid Sakshi that refuses every request."""

    workspace_root: Path | None = None
    environment: list[str] = field(default_factory=list)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_alerts: list[str] = field(default_factory=list)
    repositories: dict[str, ReviewedRepository] = field(default_factory=dict)


def _fail(key: str, requirement: str, path: Path) -> NoReturn:
    raise SystemExit(f"Invalid '{key}' in {path} ({requirement})")


def _missing(key: str, path: Path, why: str = "") -> NoReturn:
    detail = f" ({why})" if why else ""
    raise SystemExit(f"Missing '{key}' in {path}{detail}")


def _unique_keys(path: Path) -> Callable[[list[tuple[str, object]]],
                                          dict[str, object]]:
    """Refuse a key written twice in one object while the file is read,
    naming the key as written, before the structure is known."""

    def hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                raise SystemExit(f"Invalid '{key}' in {path}: written twice")
            seen.add(key)
        return dict(pairs)

    return hook


def _utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _absolute_path(key: str, value: object, path: Path) -> Path:
    if (not isinstance(value, str) or "\x00" in value or "\n" in value
            or not _utf8_encodable(value)
            or not Path(value).is_absolute()):
        _fail(key, "must be an absolute path", path)
    return Path(value)


def _optional_text(key: str, data: dict[str, object],
                   path: Path) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str) or not value:
        _fail(key, "must be a non-empty string", path)
    return value


def _role(key: str, value: object, path: Path) -> RoleSettings:
    if not isinstance(value, dict):
        _fail(key, "must be an object", path)
    for name in value:
        if name not in _ROLE_KEYS and name not in _ROLE_OPTIONAL:
            _fail(f"{key}.{name}", "unknown key", path)
    for name in _ROLE_KEYS:
        if name not in value:
            _missing(f"{key}.{name}", path)
    seed = _absolute_path(f"{key}.seed", value["seed"], path)
    source = value.get("source")
    if source is not None and (
            not isinstance(source, str) or not source
            or source.startswith("-")
            or any(character.isspace() for character in source)
            or "\x00" in source):
        _fail(f"{key}.source", "must be a git address without whitespace",
              path)
    if (source is not None and not source.startswith("/")
            and ":" not in source.split("/", 1)[0]):
        _fail(f"{key}.source",
              "must be an absolute path or a remote git address", path)
    user = value["user"]
    if not is_user_name(user):
        _fail(f"{key}.user", f"must match {USER_NAME}", path)
    model = value["model"]
    if not isinstance(model, str) or not model:
        _fail(f"{key}.model", "must be a non-empty string", path)
    effort = value["effort"]
    if not isinstance(effort, str) or effort not in _EFFORTS:
        _fail(f"{key}.effort", "must be one of " + ", ".join(_EFFORTS),
              path)
    head = value["head"]
    if not isinstance(head, str) or re.fullmatch(r"[0-9]{2}", head) is None:
        _fail(f"{key}.head", "must be two decimal digits", path)
    return RoleSettings(seed=seed, user=user, model=model, effort=effort,
                        head=head, source=source)


def _team(key: str, value: object, path: Path) -> ReviewTeam:
    if not isinstance(value, dict):
        _fail(key, "must be an object", path)
    for name in value:
        if name not in _TEAM_KEYS:
            _fail(f"{key}.{name}", "unknown key", path)
    for name in _TEAM_REQUIRED:
        if name not in value:
            _missing(f"{key}.{name}", path)
    memory = _absolute_path(f"{key}.memory", value["memory"], path)
    deadline = value["deadline_seconds"]
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        _fail(f"{key}.deadline_seconds", "must be a finite number above 0",
              path)
    try:
        finite = math.isfinite(deadline)
    except OverflowError:
        finite = False
    if not finite or deadline <= 0:
        _fail(f"{key}.deadline_seconds", "must be a finite number above 0",
              path)
    roles: dict[str, RoleSettings] = {}
    heads: dict[str, str] = {}
    if "roles" in value:
        entries = value["roles"]
        if not isinstance(entries, dict):
            _fail(f"{key}.roles", "must be an object", path)
        for role_name, role in entries.items():
            if not isinstance(role_name, str) or not is_slug(role_name):
                _fail(f"{key}.roles.{role_name}", "must be a slug", path)
            role_key = f"{key}.roles.{role_name}"
            settings = _role(role_key, role, path)
            if settings.head in heads:
                _fail(f"{role_key}.head",
                      f"names the same head as '{heads[settings.head]}'",
                      path)
            heads[settings.head] = role_name
            roles[role_name] = settings
    musafir = None
    if "musafir" in value:
        musafir = _role(f"{key}.musafir", value["musafir"], path)
        if musafir.head in heads:
            _fail(f"{key}.musafir.head",
                  f"names the same head as '{heads[musafir.head]}'", path)
    return ReviewTeam(memory=memory,
                      deadline_seconds=deadline, roles=roles,
                      musafir=musafir)


def _repository(name: str, value: object, path: Path) -> ReviewedRepository:
    key = f"repositories.{name}"
    if not isinstance(value, dict):
        _fail(key, "must be an object", path)
    for entry in value:
        if entry not in _REPOSITORY_KEYS:
            _fail(f"{key}.{entry}", "unknown key", path)
    for entry in _REPOSITORY_KEYS:
        if entry not in value:
            _missing(f"{key}.{entry}", path)
    checkout = _absolute_path(f"{key}.checkout", value["checkout"], path)
    team = _team(f"{key}.team", value["team"], path)
    return ReviewedRepository(checkout=checkout, team=team)


def load_sakshi_settings(path: Path) -> SakshiSettings:
    """Read and validate Sakshi's settings file, refusing a bad one by
    key with the file named. No value is converted: a string stays a
    string, an integer an integer; paths become paths, and the deadline
    stays as written, an integer or a float. A path's existence is
    never checked."""
    if not (path.exists() or path.is_symlink()):
        return SakshiSettings()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"Cannot read settings file {path}: {e}")
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys(path))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Settings file is not valid JSON: {path}\n{e}")
    except (ValueError, RecursionError) as e:
        raise SystemExit(f"Cannot read settings file {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid settings in {path} (must be an object)")
    for key in data:
        if key not in _TOP_LEVEL:
            _fail(key, "unknown key", path)

    workspace_root = None
    if "workspace_root" in data:
        workspace_root = _absolute_path("workspace_root",
                                        data["workspace_root"], path)

    environment: list[str] = []
    if "environment" in data:
        names = data["environment"]
        if not isinstance(names, list):
            _fail("environment", "must be a list", path)
        seen: list[str] = []
        for name in names:
            if name in _REFUSED_ENVIRONMENT_NAMES:
                _fail("environment",
                      "a role never inherits the parent session's "
                      "messaging or identity", path)
            if (not isinstance(name, str)
                    or re.fullmatch(_ENVIRONMENT_NAME, name) is None):
                _fail("environment", f"must match {_ENVIRONMENT_NAME}", path)
            for other in seen:
                if name == other:
                    _fail("environment", "must not repeat a name", path)
            seen.append(name)
        environment = names

    telegram_bot_token = _optional_text("telegram_bot_token", data, path)
    telegram_chat_id = _optional_text("telegram_chat_id", data, path)

    telegram_alerts: list[str] = []
    if "telegram_alerts" in data:
        alerts = data["telegram_alerts"]
        if not isinstance(alerts, list):
            _fail("telegram_alerts", "must be a list", path)
        for alert in alerts:
            if not isinstance(alert, str):
                _fail("telegram_alerts", "must be a string", path)
        telegram_alerts = alerts

    repositories: dict[str, ReviewedRepository] = {}
    if "repositories" in data:
        entries = data["repositories"]
        if not isinstance(entries, dict):
            _fail("repositories", "must be an object", path)
        spelled: list[str] = []
        for name, entry in entries.items():
            if not isinstance(name, str) or not is_repository(name):
                _fail(f"repositories.{name}", "must be owner/name", path)
            for other in spelled:
                if same_repository(name, other):
                    _fail(f"repositories.{name}",
                          f"differs only in case from {other!r}", path)
            spelled.append(name)
            repositories[name] = _repository(name, entry, path)

    if repositories and workspace_root is None:
        _missing("workspace_root", path, "required when any repository is listed")

    # one seed directory names one source, across every team
    sources: dict[Path, tuple[str, str]] = {}
    for name, repository in repositories.items():
        team = repository.team
        entries = [(f"repositories.{name}.team.roles.{role_name}", role)
                   for role_name, role in team.roles.items()]
        if team.musafir is not None:
            entries.append((f"repositories.{name}.team.musafir",
                            team.musafir))
        for key, role in entries:
            if role.source is None:
                continue
            try:
                identity = role.seed_identity()
            except (RuntimeError, OSError):
                _fail(f"{key}.seed", "must be a path that can be resolved",
                      path)
            first = sources.get(identity)
            if first is None:
                sources[identity] = (role.source, key)
            elif first[0] != role.source:
                _fail(f"{key}.source",
                      f"names a second source for the seed of "
                      f"'{first[1]}'", path)
    return SakshiSettings(
        workspace_root=workspace_root,
        environment=environment,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_alerts=telegram_alerts,
        repositories=repositories,
    )

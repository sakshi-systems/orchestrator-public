"""Sakshi's settings file: what loads, what refuses, and the file named
in every refusal."""

import copy
import json
import os
import pathlib
from pathlib import Path

import pytest

from orchestrator.sakshi_settings import (
    SETTINGS_NAME,
    RoleSettings,
    SakshiSettings,
    load_sakshi_settings,
)


def _settings() -> dict:
    return {
        "workspace_root": "/srv/sakshi",
        "environment": ["ANTHROPIC_API_KEY"],
        "telegram_bot_token": "123:abc",
        "telegram_chat_id": "42",
        "telegram_alerts": ["review-failed"],
        "repositories": {
            "owner/name": {
                "checkout": "/srv/checkouts/name",
                "team": {
                    "memory": "/srv/memory/name",
                    "deadline_seconds": 30,
                    "roles": {
                        "architect": {
                            "seed": "/srv/seeds/architect",
                            "user": "review_arch",
                            "model": "claude-3",
                            "effort": "high",
                            "head": "01",
                        }
                    },
                    "musafir": {
                        "seed": "/srv/seeds/musafir",
                        "user": "review_musafir",
                        "model": "claude-3",
                        "effort": "medium",
                        "head": "04",
                    },
                },
            }
        },
    }


def _team(**changes) -> dict:
    data = _settings()
    data["repositories"]["owner/name"]["team"].update(changes)
    return data


def _role(**changes) -> dict:
    data = _settings()
    data["repositories"]["owner/name"]["team"]["roles"]["architect"].update(
        changes)
    return data


def _settings_file(tmp_path, payload) -> Path:
    path = tmp_path / SETTINGS_NAME
    if isinstance(payload, str):
        path.write_text(payload)
    else:
        path.write_text(json.dumps(payload))
    return path


def _refused(tmp_path, payload, *needles) -> None:
    path = _settings_file(tmp_path, payload)
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    message = str(raised.value)
    assert str(path) in message
    for needle in needles:
        assert needle in message


def test_an_absent_file_is_no_repository(tmp_path) -> None:
    assert SETTINGS_NAME == "sakshi.json"
    settings = load_sakshi_settings(tmp_path / SETTINGS_NAME)
    assert settings == SakshiSettings()
    assert settings.repositories == {}


def test_an_empty_object_loads_empty(tmp_path) -> None:
    settings = load_sakshi_settings(_settings_file(tmp_path, {}))
    assert settings == SakshiSettings()


def test_invalid_json_refuses(tmp_path) -> None:
    _refused(tmp_path, "{not json", "not valid JSON")


@pytest.mark.parametrize(
    "payload", [["owner/name"], 5, True, '"owner/name"']
)
def test_a_non_object_refuses(tmp_path, payload) -> None:
    _refused(tmp_path, payload, "must be an object")


def _unknown_at(build) -> dict:
    return build(_settings())


UNKNOWN_KEYS = [
    (lambda data: data.update(extra=1),
     "'extra'"),
    (lambda data: data["repositories"]["owner/name"].update(extra=1),
     "'repositories.owner/name.extra'"),
    (lambda data: data["repositories"]["owner/name"]["team"].update(extra=1),
     "'repositories.owner/name.team.extra'"),
    (lambda data: data["repositories"]["owner/name"]["team"]["roles"]
        ["architect"].update(extra=1),
     "'repositories.owner/name.team.roles.architect.extra'"),
    (lambda data: data["repositories"]["owner/name"]["team"]["musafir"]
        .update(extra=1),
     "'repositories.owner/name.team.musafir.extra'"),
    (lambda data: data["repositories"]["owner/name"]["team"].update(
        slug="architects"),
     "'repositories.owner/name.team.slug'"),
    (lambda data: data["repositories"]["owner/name"]["team"].update(
        composer={}),
     "'repositories.owner/name.team.composer'"),
]


@pytest.mark.parametrize(("change", "key"), UNKNOWN_KEYS)
def test_an_unknown_key_at_each_level_refuses(tmp_path, change, key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key, "unknown key")


@pytest.mark.parametrize(
    "deadline", [True, 0, -1, "5", float("nan"), float("inf")]
)
def test_a_bad_deadline_refuses(tmp_path, deadline) -> None:
    _refused(tmp_path, _team(deadline_seconds=deadline),
             "deadline_seconds", "must be a finite number above 0")


@pytest.mark.parametrize("user", ["-u", "a" * 32, "User", ""])
def test_a_bad_user_refuses(tmp_path, user) -> None:
    _refused(tmp_path, _role(user=user), "user", "must match")


@pytest.mark.parametrize("effort", ["extreme", ""])
def test_an_unknown_effort_refuses(tmp_path, effort) -> None:
    _refused(tmp_path, _role(effort=effort), "effort",
             "low, medium, high, xhigh, max")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_each_effort_loads(tmp_path, effort) -> None:
    settings = load_sakshi_settings(
        _settings_file(tmp_path, _role(effort=effort)))
    team = settings.repositories["owner/name"].team
    assert team.roles["architect"].effort == effort


def _relative_paths():
    def top(data):
        data["workspace_root"] = "relative/root"

    def checkout(data):
        data["repositories"]["owner/name"]["checkout"] = "relative/checkout"

    def memory(data):
        data["repositories"]["owner/name"]["team"]["memory"] = "relative/memory"

    def seed(data):
        data["repositories"]["owner/name"]["team"]["roles"]["architect"][
            "seed"] = "relative/seed"

    def musafir_seed(data):
        data["repositories"]["owner/name"]["team"]["musafir"][
            "seed"] = "relative/seed"

    return [
        (top, "'workspace_root'"),
        (checkout, "'repositories.owner/name.checkout'"),
        (memory, "'repositories.owner/name.team.memory'"),
        (seed, "'repositories.owner/name.team.roles.architect.seed'"),
        (musafir_seed, "'repositories.owner/name.team.musafir.seed'"),
    ]


@pytest.mark.parametrize(("change", "key"), _relative_paths())
def test_a_relative_path_refuses(tmp_path, change, key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key, "must be an absolute path")


def _nul_paths():
    def top(data):
        data["workspace_root"] = "/srv/a\x00b"

    def checkout(data):
        data["repositories"]["owner/name"]["checkout"] = "/srv/a\x00b"

    def memory(data):
        data["repositories"]["owner/name"]["team"]["memory"] = "/srv/a\x00b"

    def seed(data):
        data["repositories"]["owner/name"]["team"]["roles"]["architect"][
            "seed"] = "/srv/a\x00b"

    return [
        (top, "'workspace_root'"),
        (checkout, "'repositories.owner/name.checkout'"),
        (memory, "'repositories.owner/name.team.memory'"),
        (seed, "'repositories.owner/name.team.roles.architect.seed'"),
    ]


@pytest.mark.parametrize(("change", "key"), _nul_paths())
def test_a_path_holding_a_nul_refuses(tmp_path, change, key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key, "must be an absolute path")


def _path_changes(value: str, sourced_role: bool = False):
    def top(data):
        data["workspace_root"] = value

    def checkout(data):
        data["repositories"]["owner/name"]["checkout"] = value

    def memory(data):
        data["repositories"]["owner/name"]["team"]["memory"] = value

    def seed(data):
        data["repositories"]["owner/name"]["team"]["roles"]["architect"][
            "seed"] = value
        if sourced_role:
            data["repositories"]["owner/name"]["team"]["roles"]["architect"][
                "source"] = "/srv/origin.git"

    return [
        (top, "'workspace_root'"),
        (checkout, "'repositories.owner/name.checkout'"),
        (memory, "'repositories.owner/name.team.memory'"),
        (seed, "'repositories.owner/name.team.roles.architect.seed'"),
    ]


@pytest.mark.parametrize(("change", "key"),
                         _path_changes("/srv/a\nb"))
def test_a_path_holding_a_newline_refuses(tmp_path, change, key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key, "must be an absolute path")


@pytest.mark.parametrize(("change", "key"),
                         _path_changes("/srv/a\ud800b",
                                       sourced_role=True))
def test_a_path_holding_a_lone_surrogate_refuses(
        tmp_path, change, key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key, "must be an absolute path")


BAD_ENVIRONMENT = [
    (["MY VAR"], "must match"),
    (["1ST"], "must match"),
    (["PATH", "PATH"], "must not repeat"),
    ("PATH", "must be a list"),
    (["CLAUDE_CODE_MESSAGING_SOCKET"], "never inherits"),
    (["CLAUDE_CODE_MESSAGING_TOKEN"], "never inherits"),
    (["CLAUDE_CODE_SESSION_ID"], "never inherits"),
]


@pytest.mark.parametrize(("value", "needle"), BAD_ENVIRONMENT)
def test_bad_environment_refuses(tmp_path, value, needle) -> None:
    _refused(tmp_path, {**_settings(), "environment": value},
             "'environment'", needle)


def test_two_repository_keys_differing_only_in_case_refuse(tmp_path) -> None:
    data = _settings()
    data["repositories"]["Owner/name"] = data["repositories"]["owner/name"]
    _refused(tmp_path, data, "'repositories.Owner/name'",
             "differs only in case")


def test_a_repository_without_team_refuses(tmp_path) -> None:
    data = _settings()
    del data["repositories"]["owner/name"]["team"]
    _refused(tmp_path, data, "'repositories.owner/name.team'")


def test_a_team_without_a_musafir_loads(tmp_path) -> None:
    data = _settings()
    del data["repositories"]["owner/name"]["team"]["musafir"]
    settings = load_sakshi_settings(_settings_file(tmp_path, data))
    team = settings.repositories["owner/name"].team
    assert team.musafir is None
    assert team.roles["architect"].model == "claude-3"


def test_a_team_without_roles_loads(tmp_path) -> None:
    data = _settings()
    del data["repositories"]["owner/name"]["team"]["roles"]
    settings = load_sakshi_settings(_settings_file(tmp_path, data))
    team = settings.repositories["owner/name"].team
    assert team.roles == {}
    assert team.musafir is not None


def test_a_role_lacking_model_refuses(tmp_path) -> None:
    data = _settings()
    del data["repositories"]["owner/name"]["team"]["roles"]["architect"][
        "model"]
    _refused(tmp_path, data,
             "'repositories.owner/name.team.roles.architect.model'")


def test_a_repository_without_workspace_root_refuses(tmp_path) -> None:
    data = _settings()
    del data["workspace_root"]
    _refused(tmp_path, data, "'workspace_root'",
             "required when any repository is listed")


def test_full_settings_load_as_read(tmp_path) -> None:
    settings = load_sakshi_settings(_settings_file(tmp_path, _settings()))
    assert settings.workspace_root == Path("/srv/sakshi")
    assert settings.environment == ["ANTHROPIC_API_KEY"]
    assert settings.telegram_bot_token == "123:abc"
    assert settings.telegram_chat_id == "42"
    assert settings.telegram_alerts == ["review-failed"]
    repository = settings.repositories["owner/name"]
    assert repository.checkout == Path("/srv/checkouts/name")
    team = repository.team
    assert team.memory == Path("/srv/memory/name")
    assert team.deadline_seconds == 30.0
    assert team.roles["architect"] == RoleSettings(
        seed=Path("/srv/seeds/architect"), user="review_arch",
        model="claude-3", effort="high", head="01")
    assert team.musafir == RoleSettings(
        seed=Path("/srv/seeds/musafir"), user="review_musafir",
        model="claude-3", effort="medium", head="04")


MISSING_REQUIRED = [
    (lambda d: d["repositories"]["owner/name"].pop("checkout"),
     "'repositories.owner/name.checkout'"),
    (lambda d: d["repositories"]["owner/name"]["team"].pop("memory"),
     "'repositories.owner/name.team.memory'"),
    (lambda d: d["repositories"]["owner/name"]["team"].pop(
        "deadline_seconds"),
     "'repositories.owner/name.team.deadline_seconds'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["roles"]
        ["architect"].pop("seed"),
     "'repositories.owner/name.team.roles.architect.seed'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["roles"]
        ["architect"].pop("user"),
     "'repositories.owner/name.team.roles.architect.user'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["roles"]
        ["architect"].pop("effort"),
     "'repositories.owner/name.team.roles.architect.effort'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["roles"]
        ["architect"].pop("head"),
     "'repositories.owner/name.team.roles.architect.head'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["musafir"]
        .pop("head"),
     "'repositories.owner/name.team.musafir.head'"),
]


@pytest.mark.parametrize(("change", "key"), MISSING_REQUIRED)
def test_every_required_key_missing_refuses(tmp_path, change, key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key)


def test_bytes_that_are_not_utf_8_refuse(tmp_path) -> None:
    path = tmp_path / SETTINGS_NAME
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    message = str(raised.value)
    assert str(path) in message
    assert "Cannot read settings file" in message


def test_a_directory_at_the_path_refuses(tmp_path) -> None:
    path = tmp_path / SETTINGS_NAME
    path.mkdir()
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    assert str(path) in str(raised.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_an_unreadable_file_refuses(tmp_path) -> None:
    path = tmp_path / SETTINGS_NAME
    path.write_text("{}")
    path.chmod(0o000)
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    assert str(path) in str(raised.value)


def test_nesting_too_deep_refuses(tmp_path) -> None:
    _refused(tmp_path, "[" * 100000 + "]" * 100000)


def test_an_integer_too_large_for_the_reader_refuses(tmp_path) -> None:
    _refused(tmp_path, "9" * 5000)


def test_a_deadline_integer_too_large_for_float_refuses(tmp_path) -> None:
    _refused(tmp_path, _team(deadline_seconds=10**400),
             "deadline_seconds", "must be a finite number above 0")


def test_a_symlink_that_leads_nowhere_refuses(tmp_path) -> None:
    path = tmp_path / SETTINGS_NAME
    path.symlink_to(tmp_path / "nowhere")
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    assert str(path) in str(raised.value)


def test_a_key_written_twice_at_the_top_level_refuses(tmp_path) -> None:
    _refused(tmp_path, '{"environment": [], "environment": ["A"]}',
             "'environment'", "written twice")


def test_a_key_written_twice_inside_a_role_refuses(tmp_path) -> None:
    text = json.dumps(_role(model="claude-arch")).replace(
        '"model": "claude-arch"',
        '"model": "claude-arch", "model": "claude-arch"')
    _refused(tmp_path, text, "'model'", "written twice")


NON_OBJECT_SITES = [
    (lambda d: d.update(repositories=[]), "'repositories'"),
    (lambda d: d.update(repositories=None), "'repositories'"),
    (lambda d: d["repositories"].update({"owner/name": 5}),
     "'repositories.owner/name'"),
    (lambda d: d["repositories"].update({"owner/name": None}),
     "'repositories.owner/name'"),
    (lambda d: d["repositories"]["owner/name"]["team"].update(roles="x"),
     "'repositories.owner/name.team.roles'"),
    (lambda d: d["repositories"]["owner/name"]["team"].update(roles=None),
     "'repositories.owner/name.team.roles'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["roles"].update(
        {"architect": 3}),
     "'repositories.owner/name.team.roles.architect'"),
    (lambda d: d["repositories"]["owner/name"]["team"]["roles"].update(
        {"architect": None}),
     "'repositories.owner/name.team.roles.architect'"),
    (lambda d: d["repositories"]["owner/name"]["team"].update(musafir=[]),
     "'repositories.owner/name.team.musafir'"),
    (lambda d: d["repositories"]["owner/name"]["team"].update(musafir=None),
     "'repositories.owner/name.team.musafir'"),
]


@pytest.mark.parametrize(("change", "key"), NON_OBJECT_SITES)
def test_a_non_object_at_each_nested_site_refuses(tmp_path, change,
                                                  key) -> None:
    data = _settings()
    change(data)
    _refused(tmp_path, data, key, "must be an object")


@pytest.mark.parametrize("model", ["", 5])
def test_a_role_model_empty_or_not_a_string_refuses(tmp_path, model) -> None:
    _refused(tmp_path, _role(model=model), "model",
             "must be a non-empty string")


TELEGRAM_BAD = [
    ({"telegram_bot_token": ""}, "'telegram_bot_token'",
     "must be a non-empty string"),
    ({"telegram_bot_token": 5}, "'telegram_bot_token'",
     "must be a non-empty string"),
    ({"telegram_chat_id": ""}, "'telegram_chat_id'",
     "must be a non-empty string"),
    ({"telegram_chat_id": 5}, "'telegram_chat_id'",
     "must be a non-empty string"),
    ({"telegram_alerts": "x"}, "'telegram_alerts'", "must be a list"),
    ({"telegram_alerts": [5]}, "'telegram_alerts'", "must be a string"),
]


@pytest.mark.parametrize(("overrides", "key", "needle"), TELEGRAM_BAD)
def test_bad_telegram_keys_refuse(tmp_path, overrides, key, needle) -> None:
    _refused(tmp_path, {**_settings(), **overrides}, key, needle)


@pytest.mark.parametrize("head", [7, "1", "123", "ab", "", "12\n", "٠١"])
def test_a_head_that_is_not_two_digits_refuses(tmp_path, head) -> None:
    _refused(tmp_path, _role(head=head),
             "roles.architect.head", "must be two decimal digits")


def test_a_musafir_head_that_is_not_two_digits_refuses(tmp_path) -> None:
    data = _team()
    data["repositories"]["owner/name"]["team"]["musafir"]["head"] = "4"
    _refused(tmp_path, data, "team.musafir.head",
             "must be two decimal digits")


def test_two_roles_on_one_head_refuse(tmp_path) -> None:
    data = _settings()
    team = data["repositories"]["owner/name"]["team"]
    team["roles"]["razor"] = dict(team["roles"]["architect"])
    _refused(tmp_path, data, "team.roles.razor.head",
             "names the same head as 'architect'")


def test_a_role_and_the_musafir_on_one_head_refuse(tmp_path) -> None:
    data = _settings()
    data["repositories"]["owner/name"]["team"]["musafir"]["head"] = "01"
    _refused(tmp_path, data, "team.musafir.head",
             "names the same head as 'architect'")


@pytest.mark.parametrize("source", ["", "-x", "with space", "a\tb",
                                     "a\nb", "a\x00b", 5])
def test_a_bad_source_refuses(tmp_path, source) -> None:
    _refused(tmp_path, _role(source=source), "roles.architect.source",
             "must be a git address")


def test_a_source_loads_and_is_kept(tmp_path) -> None:
    settings = load_sakshi_settings(_settings_file(
        tmp_path, _role(source="https://github.com/sakshi-systems/razor.git")))
    role = settings.repositories["owner/name"].team.roles["architect"]
    assert role.source == "https://github.com/sakshi-systems/razor.git"


def _looping_resolve(monkeypatch) -> None:
    def looping(self, strict=False):
        raise RuntimeError("Symlink loop from '/srv/x'")

    monkeypatch.setattr(pathlib.Path, "resolve", looping)


def test_a_seed_that_cannot_resolve_refuses_by_key(
        tmp_path, monkeypatch) -> None:
    _looping_resolve(monkeypatch)
    path = _settings_file(tmp_path, _role(source="/srv/origin.git"))
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    message = str(raised.value)
    assert str(path) in message
    assert "'repositories.owner/name.team.roles.architect.seed'" in message
    assert "must be a path that can be resolved" in message
    assert "Symlink loop" not in message
    assert "/srv/seeds/architect" not in message


def test_an_unsourced_role_is_not_resolved(
        tmp_path, monkeypatch) -> None:
    _looping_resolve(monkeypatch)
    settings = load_sakshi_settings(
        _settings_file(tmp_path, _settings()))
    role = settings.repositories["owner/name"].team.roles["architect"]
    assert role.seed == Path("/srv/seeds/architect")
    assert role.source is None


RELATIVE_SOURCES = ["srcA", "./srcA", "../srcA", "~/srcA",
                    "sub/dir/srcA", "./a:b"]


@pytest.mark.parametrize("source", RELATIVE_SOURCES)
def test_a_relative_source_refuses(tmp_path, source) -> None:
    _refused(tmp_path, _role(source=source), "roles.architect.source",
             "must be an absolute path or a remote git address")


REMOTE_OR_ABSOLUTE_SOURCES = ["/srv/x.git", "file:///srv/x.git",
                              "https://host/x.git", "ssh://git@host/x.git",
                              "git@github.com:o/r.git", "host:path"]


@pytest.mark.parametrize("source", REMOTE_OR_ABSOLUTE_SOURCES)
def test_an_absolute_or_remote_source_loads_as_written(
        tmp_path, source) -> None:
    settings = load_sakshi_settings(
        _settings_file(tmp_path, _role(source=source)))
    role = settings.repositories["owner/name"].team.roles["architect"]
    assert role.source == source


def test_two_roles_on_one_seed_path_name_one_source(tmp_path) -> None:
    data = _settings()
    team = data["repositories"]["owner/name"]["team"]
    team["roles"]["razor"] = dict(team["roles"]["architect"])
    team["roles"]["razor"]["head"] = "02"
    team["roles"]["razor"]["user"] = "review_razor"
    settings = load_sakshi_settings(_settings_file(tmp_path, data))
    roles = settings.repositories["owner/name"].team.roles
    assert roles["razor"].seed == roles["architect"].seed
    assert roles["razor"].source is None


def test_two_roles_on_one_seed_path_with_one_source_load(tmp_path) -> None:
    data = _settings()
    team = data["repositories"]["owner/name"]["team"]
    team["roles"]["razor"] = dict(team["roles"]["architect"])
    team["roles"]["razor"]["head"] = "02"
    team["roles"]["razor"]["user"] = "review_razor"
    team["roles"]["architect"]["source"] = str(tmp_path / "origin.git")
    settings = load_sakshi_settings(_settings_file(tmp_path, data))
    roles = settings.repositories["owner/name"].team.roles
    assert roles["razor"].source is None
    assert roles["architect"].source == str(tmp_path / "origin.git")


def test_two_different_sources_on_one_seed_path_refuse(tmp_path) -> None:
    data = _settings()
    team = data["repositories"]["owner/name"]["team"]
    team["roles"]["razor"] = dict(team["roles"]["architect"])
    team["roles"]["razor"]["head"] = "02"
    team["roles"]["razor"]["user"] = "review_razor"
    team["roles"]["architect"]["source"] = str(tmp_path / "one.git")
    team["roles"]["razor"]["source"] = str(tmp_path / "two.git")
    _refused(tmp_path, data,
             "'repositories.owner/name.team.roles.razor.source'",
             "names a second source")


def _two_seed_roles(data: dict, first_seed: str, first_source: str | None,
                    second_seed: str, second_source: str | None) -> None:
    team = data["repositories"]["owner/name"]["team"]
    architect = team["roles"]["architect"]
    architect["seed"] = first_seed
    architect["source"] = first_source
    razor = dict(architect)
    razor["head"] = "02"
    razor["user"] = "review_razor"
    razor["seed"] = second_seed
    razor["source"] = second_source
    team["roles"]["razor"] = razor


def test_two_spellings_of_one_seed_with_two_sources_refuse(
        tmp_path) -> None:
    seeds_dir = tmp_path / "seeds"
    (seeds_dir / "role").mkdir(parents=True)
    data = _settings()
    _two_seed_roles(data, str(seeds_dir / "role"), str(tmp_path / "one.git"),
                    str(seeds_dir / ".." / "seeds" / "role"),
                    str(tmp_path / "two.git"))
    path = _settings_file(tmp_path, data)
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    message = str(raised.value)
    assert str(path) in message
    assert "'repositories.owner/name.team.roles.razor.source'" in message
    assert "'repositories.owner/name.team.roles.architect'" in message
    assert "names a second source" in message
    assert str(seeds_dir / "role") not in message


def test_two_spellings_of_one_seed_with_one_source_load(tmp_path) -> None:
    seeds_dir = tmp_path / "seeds"
    (seeds_dir / "role").mkdir(parents=True)
    data = _settings()
    _two_seed_roles(data, str(seeds_dir / "role"), str(tmp_path / "one.git"),
                    str(seeds_dir / ".." / "seeds" / "role"),
                    str(tmp_path / "one.git"))
    settings = load_sakshi_settings(_settings_file(tmp_path, data))
    assert len(settings.repositories["owner/name"].team.roles) == 2


def test_two_spellings_of_one_seed_with_one_sourced_role_load(
        tmp_path) -> None:
    seeds_dir = tmp_path / "seeds"
    (seeds_dir / "role").mkdir(parents=True)
    data = _settings()
    _two_seed_roles(data, str(seeds_dir / "role"), str(tmp_path / "one.git"),
                    str(seeds_dir / ".." / "seeds" / "role"), None)
    load_sakshi_settings(_settings_file(tmp_path, data))


def test_an_aliased_seed_directory_with_two_sources_refuse(
        tmp_path) -> None:
    real = tmp_path / "real"
    (real / "role").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    data = _settings()
    _two_seed_roles(data, str(alias / "role"), str(tmp_path / "one.git"),
                    str(real / "role"), str(tmp_path / "two.git"))
    path = _settings_file(tmp_path, data)
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    message = str(raised.value)
    assert str(path) in message
    assert "'repositories.owner/name.team.roles.razor.source'" in message
    assert "'repositories.owner/name.team.roles.architect'" in message
    assert "names a second source" in message
    assert str(real / "role") not in message


def test_a_seed_behind_a_link_parent_with_two_sources_load(
        tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "inner").mkdir(parents=True)
    (elsewhere / "seed").mkdir()
    (tmp_path / "seed").mkdir()
    lnk = tmp_path / "lnk"
    lnk.symlink_to(elsewhere / "inner")
    data = _settings()
    _two_seed_roles(data, str(tmp_path / "seed"), str(tmp_path / "one.git"),
                    str(lnk / ".." / "seed"), str(tmp_path / "two.git"))
    load_sakshi_settings(_settings_file(tmp_path, data))


def test_a_role_key_that_is_not_a_slug_refuses(tmp_path) -> None:
    data = _settings()
    team = data["repositories"]["owner/name"]["team"]
    team["roles"] = {"Architect": team["roles"]["architect"]}
    _refused(tmp_path, data, "team.roles", "must be a slug")


def test_a_repository_key_that_is_not_owner_name_refuses(tmp_path) -> None:
    data = _settings()
    data["repositories"] = {"ownername": data["repositories"]["owner/name"]}
    _refused(tmp_path, data, "'repositories.ownername'",
             "must be owner/name")


def test_a_third_repository_key_differing_only_in_case_refuses(
        tmp_path) -> None:
    data = _settings()
    repository = data["repositories"]["owner/name"]
    data["repositories"]["other/repo"] = copy.deepcopy(repository)
    data["repositories"]["Other/Repo"] = copy.deepcopy(repository)
    _refused(tmp_path, data, "'repositories.Other/Repo'",
             "differs only in case")


def test_a_user_of_31_characters_loads(tmp_path) -> None:
    settings = load_sakshi_settings(
        _settings_file(tmp_path, _role(user="a" * 31)))
    team = settings.repositories["owner/name"].team
    assert team.roles["architect"].user == "a" * 31


@pytest.mark.parametrize("user", ["0u", "aBc", 5])
def test_more_bad_users_refuse(tmp_path, user) -> None:
    _refused(tmp_path, _role(user=user), "user", "must match")


def test_a_name_repeated_with_another_between_refuses(tmp_path) -> None:
    _refused(tmp_path, {**_settings(), "environment": ["A", "B", "A"]},
             "'environment'", "must not repeat")


def test_environment_names_differing_only_in_case_load(tmp_path) -> None:
    settings = load_sakshi_settings(
        _settings_file(tmp_path, {**_settings(),
                                  "environment": ["PATH", "path"]}))
    assert settings.environment == ["PATH", "path"]


def test_a_non_string_environment_element_refuses(tmp_path) -> None:
    _refused(tmp_path, {**_settings(), "environment": ["A", 5]},
             "'environment'", "must match")


def test_a_path_field_that_is_not_a_string_refuses(tmp_path) -> None:
    _refused(tmp_path, {**_settings(), "workspace_root": 5},
             "'workspace_root'", "must be an absolute path")


def test_the_deadline_is_kept_as_written(tmp_path) -> None:
    integer = load_sakshi_settings(
        _settings_file(tmp_path, _team(deadline_seconds=30)))
    assert type(
        integer.repositories["owner/name"].team.deadline_seconds) is int
    assert integer.repositories["owner/name"].team.deadline_seconds == 30
    decimal = load_sakshi_settings(
        _settings_file(tmp_path, _team(deadline_seconds=30.5)))
    assert type(
        decimal.repositories["owner/name"].team.deadline_seconds) is float
    assert decimal.repositories["owner/name"].team.deadline_seconds == 30.5


def test_an_empty_file_refuses(tmp_path) -> None:
    _refused(tmp_path, "", "not valid JSON")


# a value the file holds, planted at every site that once printed one
SENTINEL = "5eCreT-token-9f1a"


def _two_roles_with_sources(first: str, second: str) -> dict:
    data = _settings()
    team = data["repositories"]["owner/name"]["team"]
    team["roles"]["razor"] = dict(team["roles"]["architect"])
    team["roles"]["razor"]["head"] = "02"
    team["roles"]["razor"]["user"] = "review_razor"
    team["roles"]["architect"]["seed"] = f"/srv/seeds/{SENTINEL}"
    team["roles"]["razor"]["seed"] = f"/srv/seeds/{SENTINEL}"
    team["roles"]["architect"]["source"] = first
    team["roles"]["razor"]["source"] = second
    return data


def _sentinel_payloads():
    def unknown_in_repository():
        data = _settings()
        data["repositories"]["owner/name"]["extra"] = {"held": SENTINEL}
        return data

    def unknown_in_team():
        data = _settings()
        data["repositories"]["owner/name"]["team"]["extra"] = {
            "held": SENTINEL}
        return data

    def unknown_in_role():
        data = _settings()
        data["repositories"]["owner/name"]["team"]["roles"]["architect"][
            "extra"] = {"held": SENTINEL}
        return data

    return [
        pytest.param(lambda: f'"{SENTINEL}"',
                     ("Invalid settings", "must be an object"),
                     id="top-level-json-string"),
        pytest.param(lambda: {**_settings(), "extra": {"held": SENTINEL}},
                     ("'extra'", "unknown key"),
                     id="unknown-top-level-key"),
        pytest.param(unknown_in_repository,
                     ("'repositories.owner/name.extra'", "unknown key"),
                     id="unknown-repository-key"),
        pytest.param(unknown_in_team,
                     ("'repositories.owner/name.team.extra'", "unknown key"),
                     id="unknown-team-key"),
        pytest.param(unknown_in_role,
                     ("'repositories.owner/name.team.roles.architect.extra'",
                      "unknown key"),
                     id="unknown-role-key"),
        pytest.param(lambda: {**_settings(),
                              "telegram_bot_token": [SENTINEL]},
                     ("'telegram_bot_token'", "must be a non-empty string"),
                     id="telegram-bot-token-list"),
        pytest.param(lambda: {**_settings(),
                              "telegram_chat_id": [SENTINEL]},
                     ("'telegram_chat_id'", "must be a non-empty string"),
                     id="telegram-chat-id-list"),
        pytest.param(lambda: {**_settings(),
                              "telegram_alerts": [[SENTINEL]]},
                     ("'telegram_alerts'", "must be a string"),
                     id="telegram-alerts-entry"),
        pytest.param(lambda: {**_settings(),
                              "environment": [f"{SENTINEL}=1"]},
                     ("'environment'", "must match"),
                     id="environment-entry"),
        pytest.param(lambda: {**_settings(),
                              "workspace_root": f"relative/{SENTINEL}"},
                     ("'workspace_root'", "must be an absolute path"),
                     id="relative-path"),
        pytest.param(lambda: _team(deadline_seconds=SENTINEL),
                     ("deadline_seconds", "must be a finite number above 0"),
                     id="deadline-string"),
        pytest.param(lambda: _role(user=SENTINEL),
                     ("roles.architect.user", "must match"),
                     id="role-user-string"),
        pytest.param(lambda: _role(effort=SENTINEL),
                     ("roles.architect.effort", "must be one of"),
                     id="role-effort-string"),
        pytest.param(lambda: _role(head=SENTINEL),
                     ("roles.architect.head", "must be two decimal digits"),
                     id="role-head-string"),
        pytest.param(lambda: _role(model=[SENTINEL]),
                     ("roles.architect.model", "must be a non-empty string"),
                     id="role-model-list"),
        pytest.param(
            lambda: _role(source=f"https://user:{SENTINEL}@host/x y"),
            ("roles.architect.source", "must be a git address"),
            id="source-address"),
        pytest.param(lambda: _two_roles_with_sources(
            "/srv/origin.git", f"https://host/{SENTINEL}.git"),
            ("'repositories.owner/name.team.roles.razor.source'",
             "names a second source"),
            id="second-source"),
    ]


@pytest.mark.parametrize(("build", "needles"), _sentinel_payloads())
def test_no_refusal_names_a_value_the_file_holds(tmp_path, build,
                                                 needles) -> None:
    path = _settings_file(tmp_path, build())
    with pytest.raises(SystemExit) as raised:
        load_sakshi_settings(path)
    message = str(raised.value)
    assert str(path) in message
    for needle in needles:
        assert needle in message
    assert SENTINEL not in message

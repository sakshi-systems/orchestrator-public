"""Configuration loading, validation and a person's run settings semantics."""

import json
from pathlib import Path

import pytest

from orchestrator.config import RUN_KEYS, OrchestratorConfig, load_config, role_runtimes
from orchestrator.ports import AgentRole


def test_role_runtimes_gives_every_role_the_runs_runtime():
    """The effective-runtime record is keyed by the four roles, each entry naming the run's runtime."""
    assert role_runtimes("codex") == {
        role.value: {"runtime": "codex"} for role in AgentRole
    }
    assert list(role_runtimes("claude")) == [role.value for role in AgentRole]
    assert role_runtimes("claude")["planner"] == {"runtime": "claude"}


def _write_config(tmp_path, monkeypatch, extra=None):
    data = dict(extra) if extra else {}
    config_path = tmp_path / "orchestrator.json"
    config_path.write_text(json.dumps(data))
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(config_path))
    return config_path


# ---------------------------------------------------------------------------
# the base: the state root, an absent default base and a named path
# ---------------------------------------------------------------------------


def _point_home_at(monkeypatch, home):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def test_absent_keys_take_their_defaults(tmp_path, monkeypatch):
    """An empty base supplies nothing, so each of the four keys takes its default."""
    _write_config(tmp_path, monkeypatch, {})
    config = load_config()
    assert config.max_iterations == 3
    assert config.usage_threshold_5h == 90.0
    assert isinstance(config.usage_threshold_5h, float)
    assert config.usage_threshold_weekly == 95.0
    assert isinstance(config.usage_threshold_weekly, float)
    assert config.enable_phase_sessions is False


def test_absent_keys_read_the_dataclass_s_defaults(tmp_path, monkeypatch):
    """An absent key takes the default the dataclass itself holds, so the two can never drift apart."""
    _write_config(tmp_path, monkeypatch, {})
    config = load_config()
    assert config.max_iterations == OrchestratorConfig.max_iterations
    assert config.usage_threshold_5h == OrchestratorConfig.usage_threshold_5h
    assert config.usage_threshold_weekly == OrchestratorConfig.usage_threshold_weekly
    assert config.enable_phase_sessions == OrchestratorConfig.enable_phase_sessions


def test_the_dataclass_holds_the_defaults_itself():
    """A configuration built with no arguments carries the four defaults, thresholds as floats."""
    config = OrchestratorConfig()
    assert config.max_iterations == 3
    assert config.usage_threshold_5h == 90.0
    assert isinstance(config.usage_threshold_5h, float)
    assert config.usage_threshold_weekly == 95.0
    assert isinstance(config.usage_threshold_weekly, float)
    assert config.enable_phase_sessions is False


def test_absent_default_base_is_no_base(tmp_path, monkeypatch):
    """With no ORCHESTRATOR_CONFIG and no file under a temporary home, the defaults alone load."""
    home = tmp_path / "home"
    home.mkdir()
    _point_home_at(monkeypatch, home)
    monkeypatch.delenv("ORCHESTRATOR_CONFIG", raising=False)

    config = load_config()

    assert config.max_iterations == 3
    assert config.usage_threshold_5h == 90.0
    assert config.usage_threshold_weekly == 95.0
    assert config.enable_phase_sessions is False
    assert config.hermes_instances == {}
    assert config.telegram_bot_token is None
    assert config.telegram_chat_id is None
    assert config.telegram_alerts == []


def test_a_file_at_the_state_root_path_is_read(tmp_path, monkeypatch):
    """The default base is read from the temporary home's state root, resolved when load_config runs."""
    home = tmp_path / "home"
    base = home / ".local" / "state" / "orchestrator" / "orchestrator.json"
    base.parent.mkdir(parents=True)
    base.write_text(json.dumps({
        "telegram_bot_token": "token",
        "telegram_chat_id": "chat",
        "hermes_instances": {"main": {"server": "127.0.0.1", "port": 8801, "key_file": "/stand/keys/main"}},
    }))
    _point_home_at(monkeypatch, home)
    monkeypatch.delenv("ORCHESTRATOR_CONFIG", raising=False)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"max_iterations": 6})

    config = load_config(project_dir, "alice")

    assert config.max_iterations == 6
    assert config.telegram_bot_token == "token"
    assert config.telegram_chat_id == "chat"
    assert config.hermes_instances == {"main": {"server": "127.0.0.1", "port": 8801, "key_file": "/stand/keys/main"}}


def test_the_base_s_telegram_alerts_load_as_given(tmp_path, monkeypatch):
    """The base's telegram_alerts list loads as the base holds it; a base without the key gives []."""
    _write_config(tmp_path, monkeypatch, {"telegram_alerts": ["task-fail", "stop", "done"]})

    config = load_config()

    assert config.telegram_alerts == ["task-fail", "stop", "done"]

    _write_config(tmp_path, monkeypatch, {})

    assert load_config().telegram_alerts == []


def test_an_explicit_path_with_no_file_exits_naming_it(tmp_path, monkeypatch):
    """ORCHESTRATOR_CONFIG naming a path where no file exists exits at the start naming that path."""
    missing = tmp_path / "elsewhere" / "orchestrator.json"
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(missing))

    with pytest.raises(SystemExit) as exc:
        load_config()

    assert str(exc.value) == f"Config file not found: {missing}"


def test_a_base_that_is_a_json_array_exits_naming_its_path(tmp_path, monkeypatch):
    """A base that parses but is not a JSON object is refused naming its path."""
    base_path = tmp_path / "orchestrator.json"
    base_path.write_text("[1, 2, 3]")
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(base_path))

    with pytest.raises(SystemExit) as exc:
        load_config()

    assert str(base_path) in str(exc.value)


def test_a_base_that_is_not_valid_json_exits_naming_its_path(tmp_path, monkeypatch):
    """A base whose text does not parse is refused naming its path."""
    base_path = tmp_path / "orchestrator.json"
    base_path.write_text("{not valid json")
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(base_path))

    with pytest.raises(SystemExit) as exc:
        load_config()

    assert str(base_path) in str(exc.value)


def test_load_config_absent_roadmap_path_is_none(tmp_path, monkeypatch):
    """Should set roadmap_path to None when the key is absent."""
    _write_config(tmp_path, monkeypatch)
    config = load_config()
    assert config.roadmap_path is None


def test_load_config_roadmap_path_passes_through(tmp_path, monkeypatch):
    """Should pass an explicit relative roadmap_path value through verbatim."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"roadmap_path": "roadmaps/alice.md"})
    config = load_config(project_dir, "alice")
    assert config.roadmap_path == "roadmaps/alice.md"


def test_load_config_roadmap_path_my_passes_through(tmp_path, monkeypatch):
    """Should pass the literal 'my' value through verbatim (resolution happens elsewhere)."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"roadmap_path": "my"})
    config = load_config(project_dir, "alice")
    assert config.roadmap_path == "my"


def test_load_config_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"roadmap_path": "/etc/passwd"})
    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert "/etc/passwd" in str(exc.value)


def test_load_config_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when roadmap_path contains a '..' segment."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"roadmap_path": "../evil.md"})
    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert "../evil.md" in str(exc.value)


def _write_run_settings(project_dir, slug, data):
    """Write the person's run settings file for `slug` under the project's .ai-factory."""
    settings_dir = project_dir / ".ai-factory" / "run-settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / f"{slug}.json"
    settings_path.write_text(json.dumps(data))
    return settings_path


def test_a_missing_person_s_file_gives_the_base_and_the_defaults(tmp_path, monkeypatch):
    """No run-settings file for the slug: the load equals the no-argument load."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    assert load_config(project_dir, "alice") == load_config()


def test_the_person_s_file_wins_over_the_base_and_the_defaults(tmp_path, monkeypatch):
    """A key of the person's file replaces the value below it; the base and the defaults supply the rest."""
    _write_config(tmp_path, monkeypatch, {"telegram_chat_id": "12345"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"max_iterations": 7, "usage_threshold_5h": 91})

    config = load_config(project_dir, "alice")

    assert config.max_iterations == 7
    assert config.usage_threshold_5h == 91.0
    assert config.telegram_chat_id == "12345"
    assert config.usage_threshold_weekly == 95.0
    assert config.enable_phase_sessions is False


def test_the_person_s_file_of_another_slug_is_never_read(tmp_path, monkeypatch):
    """Only the file named by the slug handed to load_config is read; another person's file never enters."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "bob", {"max_iterations": 9})

    config = load_config(project_dir, "alice")

    assert config.max_iterations == 3


def test_a_one_key_person_s_file_leaves_the_rest_at_their_defaults(tmp_path, monkeypatch):
    """A one-key person's file loads and every key it omits takes its default."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"max_iterations": 9})

    config = load_config(project_dir, "alice")

    assert config.max_iterations == 9
    assert config.usage_threshold_5h == 90.0
    assert config.usage_threshold_weekly == 95.0
    assert config.enable_phase_sessions is False


def test_the_person_s_file_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the person's roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"roadmap_path": "/abs"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert "/abs" in str(exc.value)


def test_the_person_s_file_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the person's roadmap_path contains '..'."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"roadmap_path": "../x"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert "../x" in str(exc.value)


def test_a_person_s_file_that_is_not_valid_json_exits_naming_it(tmp_path, monkeypatch):
    """Should raise SystemExit naming the person's file when its JSON is malformed."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    settings_path = _write_run_settings(project_dir, "alice", {})
    settings_path.write_text("{not valid json")

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert str(settings_path) in str(exc.value)


def test_a_person_s_file_that_is_not_an_object_exits_naming_it(tmp_path, monkeypatch):
    """Should raise SystemExit naming the person's file when its JSON parses but isn't an object."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    settings_path = _write_run_settings(project_dir, "alice", {})
    settings_path.write_text("[1, 2, 3]")

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert str(settings_path) in str(exc.value)


# ---------------------------------------------------------------------------
# the person's file: what it may hold and what it refuses
# ---------------------------------------------------------------------------


def test_run_keys_is_the_list_of_keys_a_person_s_file_holds():
    """RUN_KEYS names every value that shapes a run, once, in the configuration's own order."""
    assert RUN_KEYS == (
        "max_iterations",
        "plan_review_iterations",
        "planning",
        "usage_threshold_5h",
        "usage_threshold_weekly",
        "enable_phase_sessions",
        "roadmap_path",
        "runtime",
        "roles",
        "heads",
        "claude_agents",
        "codex_agents",
    )


def test_every_run_key_is_accepted_from_the_person_s_file(tmp_path, monkeypatch):
    """One person's file holding all of RUN_KEYS loads, and each value lands on the run."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    settings = {
        "max_iterations": 5,
        "plan_review_iterations": 2,
        "planning": False,
        "usage_threshold_5h": 81,
        "usage_threshold_weekly": 86,
        "enable_phase_sessions": True,
        "roadmap_path": "roadmaps/alice.md",
        "runtime": "codex",
        "roles": {"planner": {"runtime": "codex"}},
        "heads": {"builder": {"repository": str(tmp_path / "repo"), "owner": "alice", "folder": "01"}},
        "claude_agents": {"planner": {"model": "m1", "effort": "high"}},
        "codex_agents": {"implementer": {"model": "m2", "effort": "low"}},
    }
    assert set(settings) == set(RUN_KEYS)
    _write_run_settings(project_dir, "alice", settings)

    config = load_config(project_dir, "alice")

    assert config.max_iterations == 5
    assert config.plan_review_iterations == 2
    assert config.planning is False
    assert config.usage_threshold_5h == 81.0
    assert config.usage_threshold_weekly == 86.0
    assert config.enable_phase_sessions is True
    assert config.roadmap_path == "roadmaps/alice.md"
    assert config.runtime == "codex"
    assert config.roles == {"planner": {"runtime": "codex"}}
    assert config.heads == settings["heads"]
    assert config.claude_agents == {"planner": {"model": "m1", "effort": "high"}}
    assert config.codex_agents == {"implementer": {"model": "m2", "effort": "low"}}


def test_an_unknown_key_in_the_person_s_file_is_refused_naming_file_and_key(tmp_path, monkeypatch):
    """A note key the run does not know exits at the start naming the person's file and the key."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_path = _write_run_settings(project_dir, "alice", {"notes": "keep the base tidy"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert message == f"Invalid 'notes' in {person_path} (unknown key)"
    assert "keep the base tidy" not in message


def test_a_misspelled_credential_key_is_refused_without_printing_its_value(tmp_path, monkeypatch):
    """A misspelled credential key takes the unknown-key refusal; the token never prints."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    token = "1234567890:AAF-a-secret-token"
    person_path = _write_run_settings(project_dir, "alice", {"telegram_bot_tokn": token})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert message == f"Invalid 'telegram_bot_tokn' in {person_path} (unknown key)"
    assert token not in message


@pytest.mark.parametrize(
    "key, reason",
    [
        ("telegram_bot_token", "the machine's credentials, which the base file holds"),
        ("telegram_chat_id", "the machine's credentials, which the base file holds"),
        ("telegram_alerts", "the machine's list of event types, which the base file holds"),
    ],
)
def test_the_person_s_file_refuses_the_telegram_credentials(tmp_path, monkeypatch, key, reason):
    """The machine's notification keys never enter a committed file; the refusal names file and key."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_path = _write_run_settings(project_dir, "alice", {key: "secret"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert message == f"Invalid '{key}' in {person_path} ({reason})"
    assert "secret" not in message


def test_task_reviewer_in_the_person_s_file_keeps_its_retirement_message(tmp_path, monkeypatch):
    """task_reviewer keeps its retirement message naming the person's file, before the unknown-key refusal."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_path = _write_run_settings(project_dir, "alice", {"notes": "x", "task_reviewer": "run"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    assert str(exc.value) == (
        f"Invalid 'task_reviewer' in {person_path}: "
        "the setting is retired; name who reviews a task as the runtime of 'roles.code_reviewer'"
    )


def test_a_legacy_project_config_file_exits_naming_itself_and_the_person_s_file(tmp_path, monkeypatch):
    """A .ai-factory/orchestrator.json is a startup error naming itself and the person's file; never read."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    legacy_path = project_dir / ".ai-factory" / "orchestrator.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{not even json")
    _write_run_settings(project_dir, "alice", {"notes": "x"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    assert str(exc.value) == (
        f"Config file is no longer read: {legacy_path}\n"
        f"Move its values into {project_dir / '.ai-factory' / 'run-settings' / 'alice.json'} and delete it."
    )


def test_a_legacy_project_config_file_exits_even_where_no_slug_is_known(tmp_path, monkeypatch):
    """Without a slug the same exit names the run-settings path in its general form."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    legacy_path = project_dir / ".ai-factory" / "orchestrator.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("{}")

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, None)

    assert str(exc.value) == (
        f"Config file is no longer read: {legacy_path}\n"
        f"Move its values into {project_dir / '.ai-factory' / 'run-settings' / '<slug>.json'} and delete it."
    )


# ---------------------------------------------------------------------------
# the base: it refuses what shapes a run and holds the machine's keys alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", RUN_KEYS)
def test_a_run_key_in_the_base_exits_naming_the_key_the_base_and_the_person_s_file(
    tmp_path, monkeypatch, key,
):
    """Each key that shapes a run, found in the base, is a startup error naming the key,
    the base's path and the person's file the key belongs in."""
    base_path = _write_config(tmp_path, monkeypatch, {key: 1})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_file = project_dir / ".ai-factory" / "run-settings" / "alice.json"

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    assert str(exc.value) == (
        f"Invalid '{key}' in {base_path} "
        f"(a value that shapes a run; it belongs in {person_file})"
    )


def test_a_run_key_in_the_base_exits_where_no_project_is_known(tmp_path, monkeypatch):
    """Without a project directory the same exit names the person's file in its
    project-relative form, `<slug>` standing in for the slug."""
    base_path = _write_config(tmp_path, monkeypatch, {"runtime": "codex"})

    with pytest.raises(SystemExit) as exc:
        load_config()

    assert str(exc.value) == (
        f"Invalid 'runtime' in {base_path} "
        "(a value that shapes a run; it belongs in .ai-factory/run-settings/<slug>.json)"
    )


def test_a_run_key_in_the_base_exits_even_where_the_person_s_file_supplies_the_key(
    tmp_path, monkeypatch,
):
    """The base's refusal is a startup error: a valid value in the person's file
    does not let the base keep the key."""
    base_path = _write_config(tmp_path, monkeypatch, {"max_iterations": 0})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_file = _write_run_settings(project_dir, "alice", {"max_iterations": 5})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert message == (
        f"Invalid 'max_iterations' in {base_path} "
        f"(a value that shapes a run; it belongs in {person_file})"
    )


def test_a_base_key_that_is_neither_a_machine_key_nor_a_run_key_is_ignored(tmp_path, monkeypatch):
    """The base holds the machine's keys alone; anything else it carries — a note, a
    retired setting — is ignored and the load takes the defaults."""
    _write_config(tmp_path, monkeypatch, {"notes": "tidy", "task_reviewer": "run"})

    config = load_config()

    assert config.max_iterations == 3
    assert config.runtime == "claude"
    assert config.hermes_instances == {}


# ---------------------------------------------------------------------------
# runtime: strict selector, validated on the merged value
# ---------------------------------------------------------------------------


def test_load_config_runtime_absent_defaults_to_claude(tmp_path, monkeypatch):
    """Should default runtime to 'claude' when neither the base nor the person's file names it."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"max_iterations": 4})

    assert load_config().runtime == "claude"
    assert load_config(project_dir, "alice").runtime == "claude"


@pytest.mark.parametrize("person", ["claude", "codex"])
def test_load_config_runtime_from_the_person_s_file_loads(tmp_path, monkeypatch, person):
    """Should load the runtime the person's file names, both selectors alike."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"runtime": person})

    assert load_config(project_dir, "alice").runtime == person


@pytest.mark.parametrize("value", ["", None, 1, True, ["claude"], {"x": 1}, "Claude", " claude", "gpt"])
def test_load_config_runtime_invalid_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming the key and the offending value for anything but 'claude'/'codex'."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"runtime": value})
    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    assert "runtime" in str(exc.value)
    assert repr(value) in str(exc.value)


@pytest.fixture(params=["claude_agents", "codex_agents"])
def agent_key(request):
    return request.param


@pytest.fixture(params=[role.value for role in AgentRole])
def agent_role(request):
    return request.param


def test_load_config_absent_agent_blocks_are_empty(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    for config in (load_config(), load_config(project_dir, "alice")):
        assert config.claude_agents == {}
        assert config.codex_agents == {}

    _write_run_settings(project_dir, "alice", {"max_iterations": 4})
    config = load_config(project_dir, "alice")
    assert config.claude_agents == {}
    assert config.codex_agents == {}


def test_agent_block_dataclass_defaults_are_independent():
    first = OrchestratorConfig()
    second = OrchestratorConfig()
    role = AgentRole.IMPLEMENTER.value
    first.claude_agents[role] = {"model": "custom-claude"}
    assert first.codex_agents == {}
    first.codex_agents[role] = {"model": "custom-codex"}
    assert second.claude_agents == {}
    assert second.codex_agents == {}


@pytest.mark.parametrize("settings", [
    {},
    {"model": " custom-model "},
    {"effort": "custom-effort"},
    {"model": "future-model", "effort": "future-effort"},
    {"model": " ", "effort": "\t"},
])
def test_load_config_agent_settings_pass_through(tmp_path, monkeypatch, agent_key, agent_role, settings):
    block = {agent_role: settings}
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {agent_key: block})
    assert getattr(load_config(project_dir, "alice"), agent_key) == block


def test_load_config_preserves_both_agent_blocks(tmp_path, monkeypatch):
    claude = {role.value: {"model": f"claude-{role.value}"} for role in AgentRole}
    codex = {role.value: {"effort": f"codex-{role.value}"} for role in AgentRole}
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"claude_agents": claude, "codex_agents": codex})
    config = load_config(project_dir, "alice")
    assert config.claude_agents == claude
    assert config.codex_agents == codex


def _assert_invalid_agent_block(tmp_path, monkeypatch, agent_key, block, value, role=None, field=None):
    # Select the other runtime to ensure unused blocks are still validated.
    runtime = "codex" if agent_key == "claude_agents" else "claude"
    base_path = _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    supplying_path = _write_run_settings(project_dir, "alice", {"runtime": runtime, agent_key: block})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    message = str(exc.value)
    assert agent_key in message
    assert repr(value) in message
    assert str(supplying_path) in message
    assert str(base_path) not in message
    if role is not None:
        assert repr(role) in message
    if field is not None:
        assert repr(field) in message


@pytest.mark.parametrize("value", [None, [], ["model"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_agent_blocks(tmp_path, monkeypatch, agent_key, value):
    _assert_invalid_agent_block(tmp_path, monkeypatch, agent_key, value, value)


@pytest.mark.parametrize("value", [None, [], ["model"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_roles(tmp_path, monkeypatch, agent_key, agent_role, value):
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, {agent_role: value}, value, role=agent_role,
    )


def test_load_config_rejects_unknown_roles(tmp_path, monkeypatch, agent_key):
    role = "unknown_role"
    settings = {"model": "custom"}
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, {role: settings}, settings, role=role,
    )


def test_load_config_rejects_unknown_role_fields(tmp_path, monkeypatch, agent_key, agent_role):
    settings = {"model": "custom", "effort": "high", "modle": "typo"}
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, {agent_role: settings}, "typo",
        role=agent_role, field="modle",
    )


@pytest.mark.parametrize("field", ["model", "effort"])
@pytest.mark.parametrize("value", ["", None, [], ["high"], {}, {"name": "high"}, 0, 1.5, True, False])
def test_load_config_rejects_empty_or_non_string_agent_fields(
    tmp_path, monkeypatch, agent_key, agent_role, field, value,
):
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, {agent_role: {field: value}}, value,
        role=agent_role, field=field,
    )


@pytest.mark.parametrize("given", [{}, {AgentRole.IMPLEMENTER.value: {"effort": "person-effort"}}])
def test_the_person_s_agent_block_loads_as_given(tmp_path, monkeypatch, agent_key, given):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {agent_key: given})

    assert getattr(load_config(project_dir, "alice"), agent_key) == given


# ---------------------------------------------------------------------------
# roles: a role names another local runtime
# ---------------------------------------------------------------------------

_ALL_ROLES = {role.value: {"runtime": "codex"} for role in AgentRole}


def test_load_config_absent_roles_is_empty(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"max_iterations": 4})
    config = load_config(project_dir, "alice")
    assert config.roles == {}
    assert role_runtimes(config.runtime, config.roles) == {
        role.value: {"runtime": "claude"} for role in AgentRole
    }


@pytest.mark.parametrize("roles", [
    {"implementer": {"runtime": "codex"}},
    {"code_reviewer": {"runtime": "codex"}, "planner": {"runtime": "claude"}},
    dict(_ALL_ROLES),
    {},
])
def test_load_config_roles_pass_through(tmp_path, monkeypatch, roles):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": roles})

    assert load_config(project_dir, "alice").roles == roles


def test_role_runtimes_reads_roles_over_the_default():
    roles = {"implementer": {"runtime": "codex"}, "code_reviewer": {"runtime": "claude"}}

    assert role_runtimes("claude", roles) == {
        "planner": {"runtime": "claude"},
        "plan_reviewer": {"runtime": "claude"},
        "implementer": {"runtime": "codex"},
        "code_reviewer": {"runtime": "claude"},
    }
    assert role_runtimes("codex", roles)["planner"] == {"runtime": "codex"}
    assert role_runtimes("codex", roles)["implementer"] == {"runtime": "codex"}


# ---------------------------------------------------------------------------
# A head as a role's runtime

_HEAD_ENTRY = {"runtime": "head", "head": {
    "repository": "/repos/the-project", "owner": "max", "folder": "r-d",
}}


@pytest.mark.parametrize("role", ["planner", "plan_reviewer", "code_reviewer"])
def test_load_config_accepts_a_head_for_every_role_a_head_takes(tmp_path, monkeypatch, role):
    roles = {role: {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])}}
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": roles})

    config = load_config(project_dir, "alice")

    assert config.roles == roles
    assert role_runtimes("claude", config.roles)[role] == roles[role]


def test_load_config_refuses_a_head_for_the_implementer(tmp_path, monkeypatch):
    message = _assert_invalid_roles(
        tmp_path, monkeypatch,
        {"implementer": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])}},
        role="implementer", wording="a head cannot take the implementer",
    )
    assert ".ai-factory" in message  # the person's file is the one named


@pytest.mark.parametrize("head", [
    {"repository": "/repos/p", "owner": "max"},  # a missing field
    {"repository": "/repos/p", "owner": "max", "folder": "r-d", "extra": 1},  # an extra field
    {"repository": "", "owner": "max", "folder": "r-d"},  # an empty field
    {"repository": "repos/p", "owner": "max", "folder": "r-d"},  # a relative repository
    {"repository": "/repos/p", "owner": "..", "folder": "r-d"},
    {"repository": "/repos/p", "owner": "", "folder": "r-d"},
    {"repository": "/repos/p", "owner": "a/b", "folder": "r-d"},
    {"repository": "/repos/p", "owner": "max\x00", "folder": "r-d"},
    {"repository": "/repos/p", "owner": "max", "folder": "."},
    {"repository": "/repos/p\x00", "owner": "max", "folder": "r-d"},  # a NUL in the repository
    {"repository": "/repos/p\ud800", "owner": "max", "folder": "r-d"},  # not encodable
])
def test_load_config_refuses_a_defective_head_address(tmp_path, monkeypatch, head):
    _assert_invalid_roles(
        tmp_path, monkeypatch,
        {"planner": {"runtime": "head", "head": head}}, field="head",
    )


def test_a_repository_that_is_not_path_text_is_refused_with_its_own_wording(tmp_path, monkeypatch):
    message = _assert_invalid_roles(
        tmp_path, monkeypatch,
        {"planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"], repository="/repos/p\x00")}},
        role="planner", field="head",
    )
    assert "repository must be a path the filesystem can name" in message


def test_is_path_text_answers_for_an_ordinary_path_a_nul_and_a_surrogate():
    from orchestrator.locator import is_path_text

    assert is_path_text("/repos/the-project")
    assert not is_path_text("/repos/p\x00")
    assert not is_path_text("/repos/p\ud800")


def test_a_head_entry_holding_unknown_fields_is_refused(tmp_path, monkeypatch):
    _assert_invalid_roles(
        tmp_path, monkeypatch,
        {"planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"]), "instance": "main"}},
        field="instance",
    )


def test_the_person_s_head_role_loads(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    replacement = {"planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])}}
    _write_run_settings(project_dir, "alice", {"roles": replacement})

    assert load_config(project_dir, "alice").roles == replacement


def _assert_invalid_roles(tmp_path, monkeypatch, roles, value=..., role=None, field=None, wording=None):
    base_path = _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    supplying_path = _write_run_settings(project_dir, "alice", {"roles": roles})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    message = str(exc.value)
    assert "roles" in message
    if value is not ...:
        assert repr(value) in message
    assert str(supplying_path) in message
    assert str(base_path) not in message
    if role is not None:
        assert repr(role) in message
    if field is not None:
        assert repr(field) in message
    if wording is not None:
        assert wording in message
    return message


@pytest.mark.parametrize("value", [None, [], ["runtime"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_roles_key(tmp_path, monkeypatch, value):
    _assert_invalid_roles(tmp_path, monkeypatch, value, value)


@pytest.mark.parametrize("value", [None, [], ["runtime"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_role_values(tmp_path, monkeypatch, agent_role, value):
    _assert_invalid_roles(tmp_path, monkeypatch, {agent_role: value}, value, role=agent_role)


def test_load_config_rejects_unknown_role_names(tmp_path, monkeypatch):
    _assert_invalid_roles(
        tmp_path, monkeypatch,
        {"planner_reviewer": {"runtime": "claude"}}, {"runtime": "claude"}, role="planner_reviewer",
    )


@pytest.mark.parametrize("entry", [{}, {"runtime": None}, {"runtime": 1}, {"runtime": ""}])
def test_load_config_rejects_missing_or_non_string_runtimes(tmp_path, monkeypatch, agent_role, entry):
    runtime = entry.get("runtime")
    _assert_invalid_roles(
        tmp_path, monkeypatch, {agent_role: entry}, runtime, role=agent_role, field="runtime",
    )


@pytest.mark.parametrize("runtime", ["gpt", "CLAUDE", " claude", "claude/codex"])
def test_load_config_rejects_unknown_runtimes(tmp_path, monkeypatch, agent_role, runtime):
    _assert_invalid_roles(
        tmp_path, monkeypatch,
        {agent_role: {"runtime": runtime}}, runtime, role=agent_role, field="runtime",
    )


@pytest.mark.parametrize("entry", [
    {"runtime": "claude", "effort": "high"},
    {"runtime": "codex", "instance": "main"},
    {"instance": "main"},
])
def test_load_config_rejects_fields_other_than_runtime(tmp_path, monkeypatch, agent_role, entry):
    name = next(key for key in entry if key != "runtime")
    _assert_invalid_roles(
        tmp_path, monkeypatch,
        {agent_role: entry}, entry[name], role=agent_role, field=name,
    )


def test_load_config_accepts_hermes_for_the_implementer(tmp_path, monkeypatch):
    """The implementer names him like any other role; whether the project's .ai-factory
    can hold his branch is the composition root's startup check, not the file's."""
    roles = {"implementer": {"runtime": "hermes", "instance": "main"}}
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": roles})

    config = load_config(project_dir, "alice")
    assert config.roles == roles
    assert role_runtimes("claude", config.roles)["implementer"] == {"runtime": "hermes", "instance": "main"}


# ---------------------------------------------------------------------------
# hermes_instances: the machine's map of the instances a role can name
# ---------------------------------------------------------------------------

_INSTANCES = {
    "main": {"server": "127.0.0.1", "port": 8801, "key_file": "/stand/keys/main"},
    "second": {"server": "localhost", "port": 8802, "key_file": "/stand/keys/second"},
}


def test_load_config_absent_hermes_instances_is_empty(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"max_iterations": 4})

    assert load_config(project_dir, "alice").hermes_instances == {}


def test_load_config_hermes_instances_pass_through(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})

    assert load_config().hermes_instances == _INSTANCES


def test_the_person_s_file_carrying_hermes_instances_is_refused(tmp_path, monkeypatch):
    """The person's file never carries the instance map; the refusal names it, the key and the base."""
    base_path = _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    value = {"intruder": dict(_INSTANCES["main"])}
    person_path = _write_run_settings(project_dir, "alice", {"hermes_instances": value})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert message == (
        f"Invalid 'hermes_instances' in {person_path} "
        "(the base file alone holds the instance map)"
    )
    assert "intruder" not in message
    assert str(base_path) not in message


@pytest.mark.parametrize("instances", [
    None, [], ["main"], "", "scalar", 0, True,
    {"": dict(_INSTANCES["main"])},
    {"main": None}, {"main": []}, {"main": "127.0.0.1"},
    {"main": {}}, {"main": {"server": "127.0.0.1", "port": 8801}},
    {"main": {"server": "127.0.0.1", "port": 8801, "key_file": "/k", "extra": 1}},
    {"main": {"server": "", "port": 8801, "key_file": "/k"}},
    {"main": {"server": "127.0.0.1", "port": 8801, "key_file": ""}},
    {"main": {"server": 1, "port": 8801, "key_file": "/k"}},
    {"main": {"server": "127.0.0.1", "port": 8801, "key_file": 5}},
    {"main": {"server": "127.0.0.1", "port": True, "key_file": "/k"}},
    {"main": {"server": "127.0.0.1", "port": 0, "key_file": "/k"}},
    {"main": {"server": "127.0.0.1", "port": 65536, "key_file": "/k"}},
    {"main": {"server": "127.0.0.1", "port": 1.5, "key_file": "/k"}},
    {"main": {"server": "127.0.0.1", "port": "8801", "key_file": "/k"}},
])
def test_load_config_rejects_malformed_instance_maps(tmp_path, monkeypatch, instances):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": instances})

    with pytest.raises(SystemExit) as exc:
        load_config()

    assert "hermes_instances" in str(exc.value)


def test_instance_map_errors_name_the_value_and_the_file(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, {
        "hermes_instances": {"main": {"server": "s", "port": 0, "key_file": "/k"}},
    })

    with pytest.raises(SystemExit) as exc:
        load_config()

    message = str(exc.value)
    assert "hermes_instances" in message
    assert "'main'" in message and "'port'" in message and "0" in message
    assert str(path) in message


@pytest.mark.parametrize("port", [1, 65535])
def test_load_config_accepts_the_port_bounds(tmp_path, monkeypatch, port):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": {"main": {"server": "s", "port": port, "key_file": "/k"}}})

    assert load_config().hermes_instances["main"]["port"] == port


# ---------------------------------------------------------------------------
# roles on Hermes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_role", ["planner", "plan_reviewer", "implementer", "code_reviewer"])
def test_load_config_accepts_hermes_for_every_role(tmp_path, monkeypatch, agent_role):
    roles = {agent_role: {"runtime": "hermes", "instance": "main"}}
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": roles})

    config = load_config(project_dir, "alice")
    assert config.roles == roles
    assert role_runtimes("claude", config.roles)[agent_role] == {"runtime": "hermes", "instance": "main"}


@pytest.mark.parametrize("entry", [
    {"runtime": "hermes"},
    {"runtime": "hermes", "instance": None},
    {"runtime": "hermes", "instance": 5},
    {"runtime": "hermes", "instance": ""},
])
def test_load_config_rejects_hermes_without_a_usable_instance(tmp_path, monkeypatch, entry):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": {"planner": entry}})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert "roles" in message and "'planner'" in message and "instance" in message


def test_load_config_rejects_an_instance_the_map_does_not_hold(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    _write_run_settings(
        project_dir, "alice", {"roles": {"planner": {"runtime": "hermes", "instance": "elsewhere"}}},
    )

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert "'planner'" in message
    assert "'elsewhere'" in message
    assert "hermes_instances" in message


def test_the_person_s_file_may_name_a_hermes_instance_the_base_holds(tmp_path, monkeypatch):
    """A person's run settings may name a Hermes instance for a role; the base holds the map."""
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    roles = {"planner": {"runtime": "hermes", "instance": "main"}}
    _write_run_settings(project_dir, "alice", {"roles": roles})

    assert load_config(project_dir, "alice").roles == roles


def test_the_person_s_file_refuses_an_instance_the_base_does_not_hold(tmp_path, monkeypatch):
    base_path = _write_config(tmp_path, monkeypatch, {"hermes_instances": {"main": dict(_INSTANCES["main"])}})
    project_dir = tmp_path / "project"
    person_path = _write_run_settings(
        project_dir, "alice", {"roles": {"planner": {"runtime": "hermes", "instance": "intruder"}}},
    )

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert "'planner'" in message and "'intruder'" in message
    assert "hermes_instances" in message
    assert str(person_path) in message
    assert str(base_path) not in message


@pytest.mark.parametrize("entry", [
    {"runtime": "hermes", "instance": "main", "effort": "high"},
    {"runtime": "hermes", "instance": "main", "model": "x"},
])
def test_load_config_rejects_any_other_key_of_a_hermes_value(tmp_path, monkeypatch, entry):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": {"planner": entry}})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert "unknown field" in message


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_load_config_rejects_instance_in_a_local_runtime_value(tmp_path, monkeypatch, runtime):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_run_settings(project_dir, "alice", {"roles": {"planner": {"runtime": runtime, "instance": "main"}}})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    assert "unknown field" in str(exc.value)


# ---------------------------------------------------------------------------
# diagnostics name the file that supplies the effective value
# ---------------------------------------------------------------------------

PERSON_SUPPLIES_CASES = [
    ("roadmap_path", {"roadmap_path": "../x"}, "../x"),
    ("runtime", {"runtime": "gpt"}, "gpt"),
    ("max_iterations", {"max_iterations": 0}, 0),
    ("plan_review_iterations", {"plan_review_iterations": "two"}, "two"),
]


@pytest.mark.parametrize("key,person_data,value", PERSON_SUPPLIES_CASES)
def test_load_config_diagnostic_names_the_person_s_file_when_it_supplies(
    tmp_path, monkeypatch, key, person_data, value
):
    """Should name the person's file — the only file a run key can come from — when it holds the bad key."""
    base_path = _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_path = _write_run_settings(project_dir, "alice", person_data)

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    message = str(exc.value)
    assert key in message
    assert repr(value) in message
    assert str(person_path) in message
    assert str(base_path) not in message


# ---------------------------------------------------------------------------
# max_iterations: positive at startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -10])
def test_load_config_max_iterations_non_positive_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming key, value and requirement when max_iterations is below 1."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"max_iterations": value})
    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    message = str(exc.value)
    assert "max_iterations" in message
    assert repr(value) in message
    assert "must be a positive integer" in message


# ---------------------------------------------------------------------------
# the startup keys are validated on the value as read, never converted
# ---------------------------------------------------------------------------


def _assert_key_rejected(tmp_path, monkeypatch, key, value, requirement):
    """The diagnostic carries the key, the value's repr, the requirement and the person's file."""
    base_path = _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    person_path = _write_run_settings(project_dir, "alice", {key: value})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")

    assert str(exc.value) == f"Invalid '{key}' in {person_path}: {value!r} ({requirement})"
    assert str(base_path) not in str(exc.value)


@pytest.mark.parametrize("value", [True, False, 2.9, 3.0, None, "invalid", "3"])
def test_load_config_max_iterations_type_violations_raise_system_exit(tmp_path, monkeypatch, value):
    """Should reject a boolean, a float whole or not, a string, a quoted number or null."""
    _assert_key_rejected(tmp_path, monkeypatch, "max_iterations", value, "must be a positive integer")


@pytest.mark.parametrize("key", ["usage_threshold_5h", "usage_threshold_weekly"])
@pytest.mark.parametrize("value", [True, False, None, "90", "invalid", float("nan"), float("inf"), float("-inf")])
def test_load_config_threshold_type_violations_raise_system_exit(tmp_path, monkeypatch, key, value):
    """Should reject a boolean, null, a string, a quoted number and the NaN/Infinity literals."""
    _assert_key_rejected(tmp_path, monkeypatch, key, value, "must be a finite number")


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, []])
def test_load_config_enable_phase_sessions_type_violations_raise_system_exit(tmp_path, monkeypatch, value):
    """Should reject a string, a number, null and a list — 'false' must not read as True."""
    _assert_key_rejected(tmp_path, monkeypatch, "enable_phase_sessions", value, "must be a boolean")


@pytest.mark.parametrize("key", ["usage_threshold_5h", "usage_threshold_weekly"])
@pytest.mark.parametrize("value", [90, 92.5])
def test_load_config_threshold_int_and_fractional_load_as_float(tmp_path, monkeypatch, key, value):
    """Should load an integer threshold stored as a float, and a fractional one as it is."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {key: value})
    threshold = getattr(load_config(project_dir, "alice"), key)
    assert threshold == value
    assert isinstance(threshold, float)


@pytest.mark.parametrize("value", [True, False])
def test_load_config_enable_phase_sessions_bools_load_as_read(tmp_path, monkeypatch, value):
    """Should keep a JSON boolean phase-session value exactly as it is read."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"enable_phase_sessions": value})
    assert load_config(project_dir, "alice").enable_phase_sessions is value


# ---------------------------------------------------------------------------
# plan_review_iterations: non-negative int or absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "2", None])
def test_load_config_plan_review_iterations_invalid_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming key and value for a bool, negative, float, string or null."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"plan_review_iterations": value})
    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, "alice")
    message = str(exc.value)
    assert "plan_review_iterations" in message
    assert repr(value) in message


@pytest.mark.parametrize("value", [0, 2])
def test_load_config_plan_review_iterations_non_negative_int_is_valid(tmp_path, monkeypatch, value):
    """Should accept zero and a positive int, zero included."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"plan_review_iterations": value})
    assert load_config(project_dir, "alice").plan_review_iterations == value


def test_load_config_plan_review_iterations_absent_is_none(tmp_path, monkeypatch):
    """Should read None when the key is absent from both files."""
    _write_config(tmp_path, monkeypatch)
    assert load_config().plan_review_iterations is None


# planning: a JSON boolean, absent true


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_load_config_planning_type_violations_raise_system_exit(tmp_path, monkeypatch, value):
    """Should reject a string, a number, null and a list naming the key and the person's file."""
    _assert_key_rejected(tmp_path, monkeypatch, "planning", value, "must be a boolean")


@pytest.mark.parametrize("value", [True, False])
def test_load_config_planning_bools_load_as_read(tmp_path, monkeypatch, value):
    """Should hold true and false as given."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_run_settings(project_dir, "alice", {"planning": value})
    assert load_config(project_dir, "alice").planning is value


def test_load_config_planning_absent_is_true(tmp_path, monkeypatch):
    """Should read true when the key is absent from both files."""
    _write_config(tmp_path, monkeypatch)
    assert load_config().planning is True

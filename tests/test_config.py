"""Configuration loading, validation and project overlay semantics."""

import json

import pytest

from orchestrator.config import OrchestratorConfig, load_config, role_runtimes
from orchestrator.ports import AgentRole

REQUIRED_FIELDS = {
    "max_iterations": 3,
    "usage_threshold_5h": 90,
    "usage_threshold_weekly": 95,
    "enable_phase_sessions": False,
}


def test_role_runtimes_gives_every_role_the_runs_runtime():
    """The effective-runtime record is keyed by the four roles, each entry naming the run's runtime."""
    assert role_runtimes("codex") == {
        role.value: {"runtime": "codex"} for role in AgentRole
    }
    assert list(role_runtimes("claude")) == [role.value for role in AgentRole]
    assert role_runtimes("claude")["planner"] == {"runtime": "claude"}


def _write_config(tmp_path, monkeypatch, extra=None):
    data = dict(REQUIRED_FIELDS)
    if extra:
        data.update(extra)
    config_path = tmp_path / "orchestrator.json"
    config_path.write_text(json.dumps(data))
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(config_path))
    return config_path


def test_load_config_absent_roadmap_path_is_none(tmp_path, monkeypatch):
    """Should set roadmap_path to None when the key is absent."""
    _write_config(tmp_path, monkeypatch)
    config = load_config()
    assert config.roadmap_path is None


def test_load_config_roadmap_path_passes_through(tmp_path, monkeypatch):
    """Should pass an explicit relative roadmap_path value through verbatim."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "roadmaps/alice.md"})
    config = load_config()
    assert config.roadmap_path == "roadmaps/alice.md"


def test_load_config_roadmap_path_my_passes_through(tmp_path, monkeypatch):
    """Should pass the literal 'my' value through verbatim (resolution happens elsewhere)."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "my"})
    config = load_config()
    assert config.roadmap_path == "my"


def test_load_config_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "/etc/passwd"})
    with pytest.raises(SystemExit) as exc:
        load_config()
    assert "/etc/passwd" in str(exc.value)


def test_load_config_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when roadmap_path contains a '..' segment."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "../evil.md"})
    with pytest.raises(SystemExit) as exc:
        load_config()
    assert "../evil.md" in str(exc.value)


def _write_override(project_dir, data):
    override_dir = project_dir / ".ai-factory"
    override_dir.mkdir(parents=True, exist_ok=True)
    override_path = override_dir / "orchestrator.json"
    override_path.write_text(json.dumps(data))
    return override_path


def test_load_config_no_override_is_byte_identical(tmp_path, monkeypatch):
    """Should return a config equal to the no-argument load when project_dir has no override file."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    assert load_config(project_dir) == load_config()


def test_load_config_override_takes_precedence(tmp_path, monkeypatch):
    """Should take overridden keys from the project override while keeping un-overridden base values."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 7, "telegram_chat_id": "12345"})

    config = load_config(project_dir)

    assert config.max_iterations == 7
    assert config.telegram_chat_id == "12345"
    assert config.usage_threshold_5h == REQUIRED_FIELDS["usage_threshold_5h"]


def test_load_config_partial_override_does_not_require_all_keys(tmp_path, monkeypatch):
    """Should load without SystemExit when the override omits the other required keys."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 9})

    config = load_config(project_dir)

    assert config.max_iterations == 9


def test_load_config_telegram_alerts_replaces_not_merges(tmp_path, monkeypatch):
    """Should replace telegram_alerts with the override list rather than unioning it with the base."""
    _write_config(tmp_path, monkeypatch, {"telegram_alerts": ["done"]})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"telegram_alerts": ["task-fail"]})

    config = load_config(project_dir)

    assert config.telegram_alerts == ["task-fail"]


def test_load_config_override_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the override's roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"roadmap_path": "/abs"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    assert "/abs" in str(exc.value)


def test_load_config_override_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the override's roadmap_path contains '..'."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"roadmap_path": "../x"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    assert "../x" in str(exc.value)


def test_load_config_malformed_override_json_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the override file path when its JSON is malformed."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    override_path = project_dir / ".ai-factory" / "orchestrator.json"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text("{not valid json")

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    assert str(override_path) in str(exc.value)


def test_load_config_non_object_override_json_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the override file path when its JSON parses but isn't an object."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    override_path = project_dir / ".ai-factory" / "orchestrator.json"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text("[1, 2, 3]")

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    assert str(override_path) in str(exc.value)


# ---------------------------------------------------------------------------
# runtime: strict selector, validated on the merged value
# ---------------------------------------------------------------------------


def test_load_config_runtime_absent_defaults_to_claude(tmp_path, monkeypatch):
    """Should default runtime to 'claude' when neither the base nor the overlay names it."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 4})

    assert load_config().runtime == "claude"
    assert load_config(project_dir).runtime == "claude"


def test_load_config_runtime_inherited_from_base(tmp_path, monkeypatch):
    """Should inherit the base 'codex' when the overlay omits the key, not fall back to the default."""
    _write_config(tmp_path, monkeypatch, {"runtime": "codex"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 4})

    assert load_config(project_dir).runtime == "codex"


@pytest.mark.parametrize("base, overlay", [("claude", "codex"), ("codex", "claude")])
def test_load_config_runtime_overlay_wins(tmp_path, monkeypatch, base, overlay):
    """Should take the overlay's runtime over the base value in both directions."""
    _write_config(tmp_path, monkeypatch, {"runtime": base})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"runtime": overlay})

    assert load_config(project_dir).runtime == overlay


@pytest.mark.parametrize("value", ["", None, 1, True, ["claude"], {"x": 1}, "Claude", " claude", "gpt"])
def test_load_config_runtime_invalid_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming the key and the offending value for anything but 'claude'/'codex'."""
    _write_config(tmp_path, monkeypatch, {"runtime": value})
    with pytest.raises(SystemExit) as exc:
        load_config()
    assert "runtime" in str(exc.value)
    assert repr(value) in str(exc.value)


def test_load_config_runtime_invalid_overlay_over_valid_base_raises_system_exit(tmp_path, monkeypatch):
    """Should validate the merged value: an invalid overlay value is rejected even over a valid base."""
    _write_config(tmp_path, monkeypatch, {"runtime": "claude"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"runtime": None})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    assert "runtime" in str(exc.value)


@pytest.fixture(params=["claude_agents", "codex_agents"])
def agent_key(request):
    return request.param


@pytest.fixture(params=[role.value for role in AgentRole])
def agent_role(request):
    return request.param


@pytest.fixture(params=["base", "missing_overlay", "inherited_base", "overlay"])
def agent_source(request):
    return request.param


def test_load_config_absent_agent_blocks_are_empty(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    for config in (load_config(), load_config(project_dir)):
        assert config.claude_agents == {}
        assert config.codex_agents == {}

    _write_override(project_dir, {"max_iterations": 4})
    config = load_config(project_dir)
    assert config.claude_agents == {}
    assert config.codex_agents == {}


def test_agent_block_dataclass_defaults_are_independent():
    first = OrchestratorConfig(**REQUIRED_FIELDS)
    second = OrchestratorConfig(**REQUIRED_FIELDS)
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
    _write_config(tmp_path, monkeypatch, {agent_key: block})
    assert getattr(load_config(), agent_key) == block


def test_load_config_preserves_both_agent_blocks(tmp_path, monkeypatch):
    claude = {role.value: {"model": f"claude-{role.value}"} for role in AgentRole}
    codex = {role.value: {"effort": f"codex-{role.value}"} for role in AgentRole}
    _write_config(tmp_path, monkeypatch, {"claude_agents": claude, "codex_agents": codex})
    config = load_config()
    assert config.claude_agents == claude
    assert config.codex_agents == codex


def _assert_invalid_agent_block(tmp_path, monkeypatch, agent_key, source, block, value, role=None, field=None):
    # Select the other runtime to ensure unused blocks are still validated.
    runtime = "codex" if agent_key == "claude_agents" else "claude"
    base_path = _write_config(tmp_path, monkeypatch, {
        "runtime": runtime,
        agent_key: {} if source == "overlay" else block,
    })
    project_dir = None if source == "base" else tmp_path / "project"
    supplying_path = base_path
    if source == "overlay":
        supplying_path = _write_override(project_dir, {agent_key: block})
    elif source == "inherited_base":
        _write_override(project_dir, {"max_iterations": 4})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    message = str(exc.value)
    assert agent_key in message
    assert repr(value) in message
    assert str(supplying_path) in message
    if source == "overlay":
        assert str(base_path) not in message
    else:
        assert str(tmp_path / "project" / ".ai-factory" / "orchestrator.json") not in message
    if role is not None:
        assert repr(role) in message
    if field is not None:
        assert repr(field) in message


@pytest.mark.parametrize("value", [None, [], ["model"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_agent_blocks(tmp_path, monkeypatch, agent_key, agent_source, value):
    _assert_invalid_agent_block(tmp_path, monkeypatch, agent_key, agent_source, value, value)


@pytest.mark.parametrize("value", [None, [], ["model"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_roles(tmp_path, monkeypatch, agent_key, agent_role, agent_source, value):
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, agent_source, {agent_role: value}, value, role=agent_role,
    )


def test_load_config_rejects_unknown_roles(tmp_path, monkeypatch, agent_key, agent_source):
    role = "unknown_role"
    settings = {"model": "custom"}
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, agent_source, {role: settings}, settings, role=role,
    )


def test_load_config_rejects_unknown_role_fields(tmp_path, monkeypatch, agent_key, agent_role, agent_source):
    settings = {"model": "custom", "effort": "high", "modle": "typo"}
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, agent_source, {agent_role: settings}, "typo",
        role=agent_role, field="modle",
    )


@pytest.mark.parametrize("field", ["model", "effort"])
@pytest.mark.parametrize("value", ["", None, [], ["high"], {}, {"name": "high"}, 0, 1.5, True, False])
def test_load_config_rejects_empty_or_non_string_agent_fields(
    tmp_path, monkeypatch, agent_key, agent_role, agent_source, field, value,
):
    _assert_invalid_agent_block(
        tmp_path, monkeypatch, agent_key, agent_source, {agent_role: {field: value}}, value,
        role=agent_role, field=field,
    )


@pytest.mark.parametrize("replacement", [{}, {AgentRole.IMPLEMENTER.value: {"effort": "overlay-effort"}}])
def test_load_config_agent_overlay_replaces_whole_block(tmp_path, monkeypatch, agent_key, replacement):
    base = {role.value: {"model": "base-model", "effort": "base-effort"} for role in AgentRole}
    _write_config(tmp_path, monkeypatch, {agent_key: base})
    project_dir = tmp_path / "project"
    _write_override(project_dir, {agent_key: replacement})

    assert getattr(load_config(project_dir), agent_key) == replacement


def test_load_config_absent_overlay_agent_key_inherits_block(tmp_path, monkeypatch, agent_key):
    base = {role.value: {"model": "base-model", "effort": "base-effort"} for role in AgentRole}
    _write_config(tmp_path, monkeypatch, {agent_key: base})
    project_dir = tmp_path / "project"
    _write_override(project_dir, {"max_iterations": 4})

    assert getattr(load_config(project_dir), agent_key) == base


@pytest.mark.parametrize("invalid_base", [None, [], {"unknown_role": {}}, {AgentRole.IMPLEMENTER.value: {"model": ""}}])
@pytest.mark.parametrize("replacement", [{}, {AgentRole.IMPLEMENTER.value: {"model": "overlay-model"}}])
def test_load_config_valid_overlay_supersedes_invalid_base_agents(
    tmp_path, monkeypatch, agent_key, invalid_base, replacement,
):
    _write_config(tmp_path, monkeypatch, {agent_key: invalid_base})
    project_dir = tmp_path / "project"
    _write_override(project_dir, {agent_key: replacement})

    assert getattr(load_config(project_dir), agent_key) == replacement


# ---------------------------------------------------------------------------
# roles: a role names another local runtime
# ---------------------------------------------------------------------------

_ALL_ROLES = {role.value: {"runtime": "codex"} for role in AgentRole}


def test_load_config_absent_roles_is_empty(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    _write_override(project_dir, {"max_iterations": 4})
    config = load_config(project_dir)
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
    _write_config(tmp_path, monkeypatch, {"roles": roles})

    assert load_config().roles == roles


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


def test_load_config_overlay_roles_replace_the_base_wholesale(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"roles": dict(_ALL_ROLES)})
    project_dir = tmp_path / "project"
    replacement = {"planner": {"runtime": "codex"}}
    _write_override(project_dir, {"roles": replacement})

    assert load_config(project_dir).roles == replacement


# ---------------------------------------------------------------------------
# A head as a role's runtime

_HEAD_ENTRY = {"runtime": "head", "head": {
    "repository": "/repos/the-project", "owner": "max", "folder": "r-d",
}}


@pytest.mark.parametrize("role", ["planner", "plan_reviewer", "code_reviewer"])
def test_load_config_accepts_a_head_for_every_role_a_head_takes(tmp_path, monkeypatch, role):
    roles = {role: {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])}}
    _write_config(tmp_path, monkeypatch, {"roles": roles})

    config = load_config()

    assert config.roles == roles
    assert role_runtimes("claude", config.roles)[role] == roles[role]


def test_load_config_refuses_a_head_for_the_implementer(tmp_path, monkeypatch):
    message = _assert_invalid_roles(
        tmp_path, monkeypatch, "base",
        {"implementer": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])}},
        role="implementer", wording="a head cannot take the implementer",
    )
    assert ".ai-factory" not in message


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
def test_load_config_refuses_a_defective_head_address(tmp_path, monkeypatch, agent_source, head):
    _assert_invalid_roles(
        tmp_path, monkeypatch, agent_source,
        {"planner": {"runtime": "head", "head": head}}, field="head",
    )


def test_a_repository_that_is_not_path_text_is_refused_with_its_own_wording(tmp_path, monkeypatch):
    message = _assert_invalid_roles(
        tmp_path, monkeypatch, "base",
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
        tmp_path, monkeypatch, "base",
        {"planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"]), "instance": "main"}},
        field="instance",
    )


def test_an_overlay_that_names_a_head_replaces_roles_wholesale(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"roles": dict(_ALL_ROLES)})
    project_dir = tmp_path / "project"
    replacement = {"planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])}}
    _write_override(project_dir, {"roles": replacement})

    assert load_config(project_dir).roles == replacement



def _assert_invalid_roles(tmp_path, monkeypatch, source, roles, value=..., role=None, field=None, wording=None):
    base_path = _write_config(tmp_path, monkeypatch, {
        "roles": roles if source != "overlay" else {},
    })
    project_dir = None if source == "base" else tmp_path / "project"
    supplying_path = base_path
    if source == "overlay":
        supplying_path = _write_override(project_dir, {"roles": roles})
    elif project_dir is not None:
        _write_override(project_dir, {"max_iterations": 4})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)
    message = str(exc.value)
    assert "roles" in message
    if value is not ...:
        assert repr(value) in message
    assert str(supplying_path) in message
    if source == "overlay":
        assert str(base_path) not in message
    if role is not None:
        assert repr(role) in message
    if field is not None:
        assert repr(field) in message
    if wording is not None:
        assert wording in message
    return message


@pytest.mark.parametrize("value", [None, [], ["runtime"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_roles_key(tmp_path, monkeypatch, agent_source, value):
    _assert_invalid_roles(tmp_path, monkeypatch, agent_source, value, value)


@pytest.mark.parametrize("value", [None, [], ["runtime"], "", "scalar", 0, 1.5, True, False])
def test_load_config_rejects_non_object_role_values(tmp_path, monkeypatch, agent_role, agent_source, value):
    _assert_invalid_roles(tmp_path, monkeypatch, agent_source, {agent_role: value}, value, role=agent_role)


def test_load_config_rejects_unknown_role_names(tmp_path, monkeypatch, agent_source):
    _assert_invalid_roles(
        tmp_path, monkeypatch, agent_source,
        {"planner_reviewer": {"runtime": "claude"}}, {"runtime": "claude"}, role="planner_reviewer",
    )


@pytest.mark.parametrize("entry", [{}, {"runtime": None}, {"runtime": 1}, {"runtime": ""}])
def test_load_config_rejects_missing_or_non_string_runtimes(tmp_path, monkeypatch, agent_role, agent_source, entry):
    runtime = entry.get("runtime")
    _assert_invalid_roles(
        tmp_path, monkeypatch, agent_source, {agent_role: entry}, runtime, role=agent_role, field="runtime",
    )


@pytest.mark.parametrize("runtime", ["gpt", "CLAUDE", " claude", "claude/codex"])
def test_load_config_rejects_unknown_runtimes(tmp_path, monkeypatch, agent_role, agent_source, runtime):
    _assert_invalid_roles(
        tmp_path, monkeypatch, agent_source,
        {agent_role: {"runtime": runtime}}, runtime, role=agent_role, field="runtime",
    )


@pytest.mark.parametrize("entry", [
    {"runtime": "claude", "effort": "high"},
    {"runtime": "codex", "instance": "main"},
    {"instance": "main"},
])
def test_load_config_rejects_fields_other_than_runtime(tmp_path, monkeypatch, agent_role, agent_source, entry):
    name = next(key for key in entry if key != "runtime")
    _assert_invalid_roles(
        tmp_path, monkeypatch, agent_source,
        {agent_role: entry}, entry[name], role=agent_role, field=name,
    )


def test_load_config_accepts_hermes_for_the_implementer(tmp_path, monkeypatch):
    """The implementer names him like any other role; whether the project's .ai-factory
    can hold his branch is the composition root's startup check, not the file's."""
    roles = {"implementer": {"runtime": "hermes", "instance": "main"}}
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES, "roles": roles})

    config = load_config()
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
    _write_override(project_dir, {"max_iterations": 4})

    assert load_config(project_dir).hermes_instances == {}


def test_load_config_hermes_instances_pass_through(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})

    assert load_config().hermes_instances == _INSTANCES


def test_load_config_overlay_carrying_hermes_instances_is_refused(tmp_path, monkeypatch):
    base_path = _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    overlay_path = _write_override(project_dir, {"hermes_instances": {"intruder": dict(_INSTANCES["main"])}})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)

    message = str(exc.value)
    assert "hermes_instances" in message
    assert "intruder" in message
    assert str(overlay_path) in message
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
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES, "roles": roles})

    config = load_config()
    assert config.roles == roles
    assert role_runtimes("claude", config.roles)[agent_role] == {"runtime": "hermes", "instance": "main"}


@pytest.mark.parametrize("entry", [
    {"runtime": "hermes"},
    {"runtime": "hermes", "instance": None},
    {"runtime": "hermes", "instance": 5},
    {"runtime": "hermes", "instance": ""},
])
def test_load_config_rejects_hermes_without_a_usable_instance(tmp_path, monkeypatch, entry):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES, "roles": {"planner": entry}})

    with pytest.raises(SystemExit) as exc:
        load_config()

    message = str(exc.value)
    assert "roles" in message and "'planner'" in message and "instance" in message


def test_load_config_rejects_an_instance_the_map_does_not_hold(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES, "roles": {
        "planner": {"runtime": "hermes", "instance": "elsewhere"},
    }})

    with pytest.raises(SystemExit) as exc:
        load_config()

    message = str(exc.value)
    assert "'planner'" in message
    assert "'elsewhere'" in message
    assert "hermes_instances" in message


def test_load_config_accepts_an_overlay_named_hermes_instance_held_by_the_base(tmp_path, monkeypatch):
    """A project's overlay may name a Hermes instance for a role; the base holds the map."""
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES})
    project_dir = tmp_path / "project"
    roles = {"planner": {"runtime": "hermes", "instance": "main"}}
    _write_override(project_dir, {"roles": roles})

    assert load_config(project_dir).roles == roles


def test_load_config_refuses_an_overlay_instance_the_base_does_not_hold(tmp_path, monkeypatch):
    base_path = _write_config(tmp_path, monkeypatch, {"hermes_instances": {"main": dict(_INSTANCES["main"])}})
    project_dir = tmp_path / "project"
    overlay_path = _write_override(
        project_dir, {"roles": {"planner": {"runtime": "hermes", "instance": "intruder"}}},
    )

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)

    message = str(exc.value)
    assert "'planner'" in message and "'intruder'" in message
    assert "hermes_instances" in message
    assert str(overlay_path) in message
    assert str(base_path) not in message


@pytest.mark.parametrize("entry", [
    {"runtime": "hermes", "instance": "main", "effort": "high"},
    {"runtime": "hermes", "instance": "main", "model": "x"},
])
def test_load_config_rejects_any_other_key_of_a_hermes_value(tmp_path, monkeypatch, entry):
    _write_config(tmp_path, monkeypatch, {"hermes_instances": _INSTANCES, "roles": {"planner": entry}})

    with pytest.raises(SystemExit) as exc:
        load_config()

    message = str(exc.value)
    assert "unknown field" in message


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_load_config_rejects_instance_in_a_local_runtime_value(tmp_path, monkeypatch, runtime):
    _write_config(tmp_path, monkeypatch, {"roles": {"planner": {"runtime": runtime, "instance": "main"}}})

    with pytest.raises(SystemExit) as exc:
        load_config()

    assert "unknown field" in str(exc.value)


# ---------------------------------------------------------------------------
# diagnostics name the file that supplies the effective value
# ---------------------------------------------------------------------------

BASE_SUPPLIES_CASES = [
    ("roadmap_path", {"roadmap_path": "/abs"}, {"max_iterations": 4}, "/abs"),
    ("runtime", {"runtime": "gpt"}, {"max_iterations": 4}, "gpt"),
    ("max_iterations", {"max_iterations": 0}, {"telegram_chat_id": "1"}, 0),
    ("plan_review_iterations", {"plan_review_iterations": -1}, {"max_iterations": 4}, -1),
]

OVERLAY_SUPPLIES_CASES = [
    ("roadmap_path", {}, {"roadmap_path": "../x"}, "../x"),
    ("runtime", {}, {"runtime": "gpt"}, "gpt"),
    ("max_iterations", {}, {"max_iterations": 0}, 0),
    ("plan_review_iterations", {}, {"plan_review_iterations": "two"}, "two"),
]


def _assert_diagnostic_names_supplying_file(tmp_path, monkeypatch, key, base_extra, overlay_data, value):
    base_path = _write_config(tmp_path, monkeypatch, base_extra)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    overlay_path = _write_override(project_dir, overlay_data)

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)

    message = str(exc.value)
    assert key in message
    assert repr(value) in message
    if key in overlay_data:
        assert str(overlay_path) in message
        assert str(base_path) not in message
    else:
        assert str(base_path) in message
        assert str(overlay_path) not in message


@pytest.mark.parametrize("key,base_extra,overlay_data,value", BASE_SUPPLIES_CASES)
def test_load_config_diagnostic_names_base_when_base_supplies(
    tmp_path, monkeypatch, key, base_extra, overlay_data, value
):
    """Should name the base file when the overlay exists but omits the bad key."""
    _assert_diagnostic_names_supplying_file(tmp_path, monkeypatch, key, base_extra, overlay_data, value)


@pytest.mark.parametrize("key,base_extra,overlay_data,value", OVERLAY_SUPPLIES_CASES)
def test_load_config_diagnostic_names_overlay_when_overlay_supplies(
    tmp_path, monkeypatch, key, base_extra, overlay_data, value
):
    """Should name the overlay file when the overlay holds the bad key."""
    _assert_diagnostic_names_supplying_file(tmp_path, monkeypatch, key, base_extra, overlay_data, value)


# ---------------------------------------------------------------------------
# max_iterations: positive at startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -10])
def test_load_config_max_iterations_non_positive_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming key, value and requirement when max_iterations is below 1."""
    _write_config(tmp_path, monkeypatch, {"max_iterations": value})
    with pytest.raises(SystemExit) as exc:
        load_config()
    message = str(exc.value)
    assert "max_iterations" in message
    assert repr(value) in message
    assert "must be a positive integer" in message


def test_load_config_valid_max_iterations_overlay_supersedes_invalid_base(tmp_path, monkeypatch):
    """Should accept a valid overlay value over a non-positive base value."""
    _write_config(tmp_path, monkeypatch, {"max_iterations": 0})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 5})

    assert load_config(project_dir).max_iterations == 5


# ---------------------------------------------------------------------------
# the required keys are validated on the merged value, never converted
# ---------------------------------------------------------------------------


def _assert_required_key_rejected(tmp_path, monkeypatch, key, value, requirement, source):
    """The diagnostic carries the key, the value's repr, the requirement and only the supplying file."""
    base_path = _write_config(tmp_path, monkeypatch, {} if source == "overlay" else {key: value})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    overlay_path = _write_override(project_dir, {"telegram_chat_id": "1"} if source == "base" else {key: value})
    supplying_path = overlay_path if source == "overlay" else base_path

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir)

    assert str(exc.value) == f"Invalid '{key}' in {supplying_path}: {value!r} ({requirement})"


@pytest.mark.parametrize("source", ["base", "overlay"])
@pytest.mark.parametrize("value", [True, False, 2.9, 3.0, None, "invalid", "3"])
def test_load_config_max_iterations_type_violations_raise_system_exit(tmp_path, monkeypatch, value, source):
    """Should reject a boolean, a float whole or not, a string, a quoted number or null."""
    _assert_required_key_rejected(tmp_path, monkeypatch, "max_iterations", value, "must be a positive integer", source)


@pytest.mark.parametrize("source", ["base", "overlay"])
@pytest.mark.parametrize("key", ["usage_threshold_5h", "usage_threshold_weekly"])
@pytest.mark.parametrize("value", [True, False, None, "90", "invalid", float("nan"), float("inf"), float("-inf")])
def test_load_config_threshold_type_violations_raise_system_exit(tmp_path, monkeypatch, key, value, source):
    """Should reject a boolean, null, a string, a quoted number and the NaN/Infinity literals."""
    _assert_required_key_rejected(tmp_path, monkeypatch, key, value, "must be a finite number", source)


@pytest.mark.parametrize("source", ["base", "overlay"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, []])
def test_load_config_enable_phase_sessions_type_violations_raise_system_exit(tmp_path, monkeypatch, value, source):
    """Should reject a string, a number, null and a list — 'false' must not read as True."""
    _assert_required_key_rejected(tmp_path, monkeypatch, "enable_phase_sessions", value, "must be a boolean", source)


@pytest.mark.parametrize("key", ["usage_threshold_5h", "usage_threshold_weekly"])
@pytest.mark.parametrize("value", [90, 92.5])
def test_load_config_threshold_int_and_fractional_load_as_float(tmp_path, monkeypatch, key, value):
    """Should load an integer threshold stored as a float, and a fractional one as it is."""
    _write_config(tmp_path, monkeypatch, {key: value})
    threshold = getattr(load_config(), key)
    assert threshold == value
    assert isinstance(threshold, float)


@pytest.mark.parametrize("value", [True, False])
def test_load_config_enable_phase_sessions_bools_load_as_read(tmp_path, monkeypatch, value):
    """Should keep a JSON boolean phase-session value exactly as it is read."""
    _write_config(tmp_path, monkeypatch, {"enable_phase_sessions": value})
    assert load_config().enable_phase_sessions is value


@pytest.mark.parametrize("key, invalid_base, valid_overlay", [
    ("max_iterations", 0, 5),
    ("usage_threshold_5h", "90", 91),
    ("usage_threshold_weekly", True, 96),
    ("enable_phase_sessions", "false", True),
])
def test_load_config_valid_overlay_supersedes_invalid_base_required_key(
    tmp_path, monkeypatch, key, invalid_base, valid_overlay,
):
    """Should accept a valid overlay value over an invalid base value for every required key."""
    _write_config(tmp_path, monkeypatch, {key: invalid_base})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {key: valid_overlay})

    assert getattr(load_config(project_dir), key) == valid_overlay


# ---------------------------------------------------------------------------
# plan_review_iterations: non-negative int or absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "2", None])
def test_load_config_plan_review_iterations_invalid_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming key and value for a bool, negative, float, string or null."""
    _write_config(tmp_path, monkeypatch, {"plan_review_iterations": value})
    with pytest.raises(SystemExit) as exc:
        load_config()
    message = str(exc.value)
    assert "plan_review_iterations" in message
    assert repr(value) in message


@pytest.mark.parametrize("value", [0, 2])
def test_load_config_plan_review_iterations_non_negative_int_is_valid(tmp_path, monkeypatch, value):
    """Should accept zero and a positive int, zero included."""
    _write_config(tmp_path, monkeypatch, {"plan_review_iterations": value})
    assert load_config().plan_review_iterations == value


def test_load_config_plan_review_iterations_absent_is_none(tmp_path, monkeypatch):
    """Should read None when the key is absent from both files."""
    _write_config(tmp_path, monkeypatch)
    assert load_config().plan_review_iterations is None


# planning: a JSON boolean, absent true


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
@pytest.mark.parametrize("source", ["base", "overlay"])
def test_load_config_planning_type_violations_raise_system_exit(tmp_path, monkeypatch, value, source):
    """Should reject a string, a number, null and a list naming the key and the supplying file."""
    _assert_required_key_rejected(tmp_path, monkeypatch, "planning", value, "must be a boolean", source)


@pytest.mark.parametrize("value", [True, False])
def test_load_config_planning_bools_load_as_read(tmp_path, monkeypatch, value):
    """Should hold true and false as given."""
    _write_config(tmp_path, monkeypatch, {"planning": value})
    assert load_config().planning is value


def test_load_config_planning_absent_is_true(tmp_path, monkeypatch):
    """Should read true when the key is absent from both files."""
    _write_config(tmp_path, monkeypatch)
    assert load_config().planning is True


@pytest.mark.parametrize("base,overlay,expected", [(True, False, False), (False, True, True)])
def test_load_config_planning_overlay_wins(tmp_path, monkeypatch, base, overlay, expected):
    """The overlay's value wins over the base's."""
    _write_config(tmp_path, monkeypatch, {"planning": base})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"planning": overlay})
    assert load_config(project_dir).planning is expected

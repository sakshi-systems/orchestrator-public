"""Configuration loading, validation and project overlay semantics."""

import json

import pytest

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.main import TASK_REVIEWERS
from orchestrator.ports import AgentRole

REQUIRED_FIELDS = {
    "max_iterations": 3,
    "usage_threshold_5h": 90,
    "usage_threshold_weekly": 95,
    "enable_phase_sessions": False,
}


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
    config = load_config(task_reviewers=TASK_REVIEWERS)
    assert config.roadmap_path is None


def test_load_config_roadmap_path_passes_through(tmp_path, monkeypatch):
    """Should pass an explicit relative roadmap_path value through verbatim."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "roadmaps/alice.md"})
    config = load_config(task_reviewers=TASK_REVIEWERS)
    assert config.roadmap_path == "roadmaps/alice.md"


def test_load_config_roadmap_path_my_passes_through(tmp_path, monkeypatch):
    """Should pass the literal 'my' value through verbatim (resolution happens elsewhere)."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "my"})
    config = load_config(task_reviewers=TASK_REVIEWERS)
    assert config.roadmap_path == "my"


def test_load_config_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "/etc/passwd"})
    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS)
    assert "/etc/passwd" in str(exc.value)


def test_load_config_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when roadmap_path contains a '..' segment."""
    _write_config(tmp_path, monkeypatch, {"roadmap_path": "../evil.md"})
    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS)
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
    assert load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir) == load_config(task_reviewers=TASK_REVIEWERS)


def test_load_config_override_takes_precedence(tmp_path, monkeypatch):
    """Should take overridden keys from the project override while keeping un-overridden base values."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 7, "telegram_chat_id": "12345"})

    config = load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)

    assert config.max_iterations == 7
    assert config.telegram_chat_id == "12345"
    assert config.usage_threshold_5h == REQUIRED_FIELDS["usage_threshold_5h"]


def test_load_config_partial_override_does_not_require_all_keys(tmp_path, monkeypatch):
    """Should load without SystemExit when the override omits the other required keys."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 9})

    config = load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)

    assert config.max_iterations == 9


def test_load_config_telegram_alerts_replaces_not_merges(tmp_path, monkeypatch):
    """Should replace telegram_alerts with the override list rather than unioning it with the base."""
    _write_config(tmp_path, monkeypatch, {"telegram_alerts": ["done"]})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"telegram_alerts": ["task-fail"]})

    config = load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)

    assert config.telegram_alerts == ["task-fail"]


def test_load_config_override_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the override's roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"roadmap_path": "/abs"})

    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)
    assert "/abs" in str(exc.value)


def test_load_config_override_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the override's roadmap_path contains '..'."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"roadmap_path": "../x"})

    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)
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
        load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)
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
        load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)
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

    assert load_config(task_reviewers=TASK_REVIEWERS).runtime == "claude"
    assert load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir).runtime == "claude"


def test_load_config_runtime_inherited_from_base(tmp_path, monkeypatch):
    """Should inherit the base 'codex' when the overlay omits the key, not fall back to the default."""
    _write_config(tmp_path, monkeypatch, {"runtime": "codex"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 4})

    assert load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir).runtime == "codex"


@pytest.mark.parametrize("base, overlay", [("claude", "codex"), ("codex", "claude")])
def test_load_config_runtime_overlay_wins(tmp_path, monkeypatch, base, overlay):
    """Should take the overlay's runtime over the base value in both directions."""
    _write_config(tmp_path, monkeypatch, {"runtime": base})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"runtime": overlay})

    assert load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir).runtime == overlay


@pytest.mark.parametrize("value", ["", None, 1, True, ["claude"], {"x": 1}, "Claude", " claude", "gpt"])
def test_load_config_runtime_invalid_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming the key and the offending value for anything but 'claude'/'codex'."""
    _write_config(tmp_path, monkeypatch, {"runtime": value})
    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS)
    assert "runtime" in str(exc.value)
    assert repr(value) in str(exc.value)


def test_load_config_runtime_invalid_overlay_over_valid_base_raises_system_exit(tmp_path, monkeypatch):
    """Should validate the merged value: an invalid overlay value is rejected even over a valid base."""
    _write_config(tmp_path, monkeypatch, {"runtime": "claude"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"runtime": None})

    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)
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
    for config in (load_config(task_reviewers=TASK_REVIEWERS), load_config(project_dir, task_reviewers=TASK_REVIEWERS)):
        assert config.claude_agents == {}
        assert config.codex_agents == {}

    _write_override(project_dir, {"max_iterations": 4})
    config = load_config(project_dir, task_reviewers=TASK_REVIEWERS)
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
    assert getattr(load_config(task_reviewers=TASK_REVIEWERS), agent_key) == block


def test_load_config_preserves_both_agent_blocks(tmp_path, monkeypatch):
    claude = {role.value: {"model": f"claude-{role.value}"} for role in AgentRole}
    codex = {role.value: {"effort": f"codex-{role.value}"} for role in AgentRole}
    _write_config(tmp_path, monkeypatch, {"claude_agents": claude, "codex_agents": codex})
    config = load_config(task_reviewers=TASK_REVIEWERS)
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
        load_config(project_dir, task_reviewers=TASK_REVIEWERS)
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

    assert getattr(load_config(project_dir, task_reviewers=TASK_REVIEWERS), agent_key) == replacement


def test_load_config_absent_overlay_agent_key_inherits_block(tmp_path, monkeypatch, agent_key):
    base = {role.value: {"model": "base-model", "effort": "base-effort"} for role in AgentRole}
    _write_config(tmp_path, monkeypatch, {agent_key: base})
    project_dir = tmp_path / "project"
    _write_override(project_dir, {"max_iterations": 4})

    assert getattr(load_config(project_dir, task_reviewers=TASK_REVIEWERS), agent_key) == base


@pytest.mark.parametrize("invalid_base", [None, [], {"unknown_role": {}}, {AgentRole.IMPLEMENTER.value: {"model": ""}}])
@pytest.mark.parametrize("replacement", [{}, {AgentRole.IMPLEMENTER.value: {"model": "overlay-model"}}])
def test_load_config_valid_overlay_supersedes_invalid_base_agents(
    tmp_path, monkeypatch, agent_key, invalid_base, replacement,
):
    _write_config(tmp_path, monkeypatch, {agent_key: invalid_base})
    project_dir = tmp_path / "project"
    _write_override(project_dir, {agent_key: replacement})

    assert getattr(load_config(project_dir, task_reviewers=TASK_REVIEWERS), agent_key) == replacement


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
        load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir)

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
        load_config(task_reviewers=TASK_REVIEWERS)
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

    assert load_config(task_reviewers=TASK_REVIEWERS, project_dir=project_dir).max_iterations == 5


# ---------------------------------------------------------------------------
# plan_review_iterations: non-negative int or absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "2", None])
def test_load_config_plan_review_iterations_invalid_raises_system_exit(tmp_path, monkeypatch, value):
    """Should raise SystemExit naming key and value for a bool, negative, float, string or null."""
    _write_config(tmp_path, monkeypatch, {"plan_review_iterations": value})
    with pytest.raises(SystemExit) as exc:
        load_config(task_reviewers=TASK_REVIEWERS)
    message = str(exc.value)
    assert "plan_review_iterations" in message
    assert repr(value) in message


@pytest.mark.parametrize("value", [0, 2])
def test_load_config_plan_review_iterations_non_negative_int_is_valid(tmp_path, monkeypatch, value):
    """Should accept zero and a positive int, zero included."""
    _write_config(tmp_path, monkeypatch, {"plan_review_iterations": value})
    assert load_config(task_reviewers=TASK_REVIEWERS).plan_review_iterations == value


def test_load_config_plan_review_iterations_absent_is_none(tmp_path, monkeypatch):
    """Should read None when the key is absent from both files."""
    _write_config(tmp_path, monkeypatch)
    assert load_config(task_reviewers=TASK_REVIEWERS).plan_review_iterations is None

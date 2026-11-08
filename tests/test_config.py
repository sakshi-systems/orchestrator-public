"""Unit tests for OrchestratorConfig loading: the roadmap_path guard, the strict runtime selector, and the project overlay."""

import json

import pytest

from orchestrator.config import load_config

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
    assert load_config(project_dir=project_dir) == load_config()


def test_load_config_override_takes_precedence(tmp_path, monkeypatch):
    """Should take overridden keys from the project override while keeping un-overridden base values."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 7, "telegram_chat_id": "12345"})

    config = load_config(project_dir=project_dir)

    assert config.max_iterations == 7
    assert config.telegram_chat_id == "12345"
    assert config.usage_threshold_5h == REQUIRED_FIELDS["usage_threshold_5h"]


def test_load_config_partial_override_does_not_require_all_keys(tmp_path, monkeypatch):
    """Should load without SystemExit when the override omits the other required keys."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 9})

    config = load_config(project_dir=project_dir)

    assert config.max_iterations == 9


def test_load_config_telegram_alerts_replaces_not_merges(tmp_path, monkeypatch):
    """Should replace telegram_alerts with the override list rather than unioning it with the base."""
    _write_config(tmp_path, monkeypatch, {"telegram_alerts": ["done"]})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"telegram_alerts": ["task-fail"]})

    config = load_config(project_dir=project_dir)

    assert config.telegram_alerts == ["task-fail"]


def test_load_config_override_roadmap_path_absolute_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the override's roadmap_path is absolute."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"roadmap_path": "/abs"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir=project_dir)
    assert "/abs" in str(exc.value)


def test_load_config_override_roadmap_path_dotdot_raises_system_exit(tmp_path, monkeypatch):
    """Should raise SystemExit naming the offending value when the override's roadmap_path contains '..'."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"roadmap_path": "../x"})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir=project_dir)
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
        load_config(project_dir=project_dir)
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
        load_config(project_dir=project_dir)
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
    assert load_config(project_dir=project_dir).runtime == "claude"


def test_load_config_runtime_inherited_from_base(tmp_path, monkeypatch):
    """Should inherit the base 'codex' when the overlay omits the key, not fall back to the default."""
    _write_config(tmp_path, monkeypatch, {"runtime": "codex"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 4})

    assert load_config(project_dir=project_dir).runtime == "codex"


@pytest.mark.parametrize("base, overlay", [("claude", "codex"), ("codex", "claude")])
def test_load_config_runtime_overlay_wins(tmp_path, monkeypatch, base, overlay):
    """Should take the overlay's runtime over the base value in both directions."""
    _write_config(tmp_path, monkeypatch, {"runtime": base})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"runtime": overlay})

    assert load_config(project_dir=project_dir).runtime == overlay


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
        load_config(project_dir=project_dir)
    assert "runtime" in str(exc.value)

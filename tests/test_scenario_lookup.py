from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.errors import ScenarioRefusal
from orchestrator.main import _SCENARIO_KINDS, _check_stop_pairs
from orchestrator.scenario_file import Cycle, Scenario, Step, load_scenario
from orchestrator.scenario_lookup import SCENARIOS_DIR, TEMPLATES_DIR, fill_template, resolve_scenario

BODY = "steps:\n  - walk: test\n"


def _project_scenario(tmp_path: Path, name: str) -> Path:
    path = tmp_path / ".ai-factory" / "scenarios" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BODY, encoding="utf-8")
    return path


def test_project_file_shadows_shipped_file(tmp_path):
    path = _project_scenario(tmp_path, "implement")
    resolved = resolve_scenario("implement", tmp_path)
    assert resolved.path == path
    assert resolved.shadows is True


def test_shipped_file_used_when_project_has_none(tmp_path):
    resolved = resolve_scenario("implement", tmp_path)
    assert resolved.path == SCENARIOS_DIR / "implement.yaml"
    assert resolved.shadows is False


def test_project_only_name(tmp_path):
    path = _project_scenario(tmp_path, "nightly")
    resolved = resolve_scenario("nightly", tmp_path)
    assert resolved.path == path
    assert resolved.shadows is False


def test_separator_is_refused_even_when_path_exists(tmp_path):
    _project_scenario(tmp_path, "sub/x")
    with pytest.raises(ScenarioRefusal):
        resolve_scenario("sub/x", tmp_path)


@pytest.mark.parametrize("name", ["", ".", ".."])
def test_non_component_names_are_refused(tmp_path, name):
    with pytest.raises(ScenarioRefusal):
        resolve_scenario(name, tmp_path)


def test_unknown_name_names_both_places(tmp_path):
    with pytest.raises(ScenarioRefusal) as info:
        resolve_scenario("nope", tmp_path)
    first = str(info.value).splitlines()[0]
    assert str(tmp_path / ".ai-factory" / "scenarios" / "nope.yaml") in first
    assert str(SCENARIOS_DIR / "nope.yaml") in first


@pytest.mark.parametrize("name", ["implement", "test"])
def test_shipped_files_load(name):
    path = SCENARIOS_DIR / f"{name}.yaml"
    scenario = load_scenario(path, _SCENARIO_KINDS)
    if name == "implement":
        assert scenario.steps == (
            Cycle("no-task", (
                Step("walk", "implement", {"stop": "closure-owed"}),
                Step("close", "builder", {"peer": "razor"}),
            )),
        )
    else:
        assert scenario.steps == (Step("walk", name),)
    _check_stop_pairs(path, scenario)


def test_the_shipped_closure_template_carries_each_value_it_is_filled_with():
    values = {
        "phase": "4242",
        "roadmap": "roadmaps/sentinel-roadmap.md",
        "project": "/sentinel/project",
        "peer": "repository: /sentinel/peer-repo, owner: sentinel-owner, folder: sentinel-folder",
    }

    filled = fill_template(TEMPLATES_DIR / "closure.md", values)

    for name, value in values.items():
        assert value in filled, name
        assert "{" + name + "}" not in filled, name
    # A template that loses {peer} runs on and never names the helper head.
    assert "repository: /sentinel/peer-repo, owner: sentinel-owner, folder: sentinel-folder" in filled


def _stop_walk():
    return Step("walk", "implement", {"stop": "closure-owed"})


def _close():
    return Step("close", "builder", {"peer": "razor"})


def test_a_stopping_walk_with_the_close_only_before_it_is_refused(tmp_path):
    scenario = Scenario((Cycle("no-task", (_close(), _stop_walk())),))

    with pytest.raises(ScenarioRefusal) as info:
        _check_stop_pairs(tmp_path / "scenario.yaml", scenario)

    first = str(info.value).splitlines()[0]
    assert "steps[0].cycle.steps[1].stop" in first
    assert "'closure-owed'" in first


def test_a_stopping_walk_in_a_cycle_with_no_close_is_refused(tmp_path):
    scenario = Scenario((Cycle("no-task", (_stop_walk(), Step("walk", "test"))),))

    with pytest.raises(ScenarioRefusal) as info:
        _check_stop_pairs(tmp_path / "scenario.yaml", scenario)

    first = str(info.value).splitlines()[0]
    assert "steps[0].cycle.steps[0].stop" in first
    assert "'closure-owed'" in first


def test_a_stopping_walk_that_stands_in_no_cycle_is_refused(tmp_path):
    scenario = Scenario((_stop_walk(),))

    with pytest.raises(ScenarioRefusal) as info:
        _check_stop_pairs(tmp_path / "scenario.yaml", scenario)

    first = str(info.value).splitlines()[0]
    assert "steps[0].stop" in first
    assert "stands in no cycle" in first

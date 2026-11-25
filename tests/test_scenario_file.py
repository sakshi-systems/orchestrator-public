from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.errors import ScenarioRefusal
from orchestrator.main import _SCENARIO_KINDS
from orchestrator.scenario_file import (
    Cycle,
    Scenario,
    Step,
    StepKind,
    StepOption,
    load_scenario,
)

KINDS = {
    "walk": StepKind(
        values=("implement", "test"),
        options={"stop": StepOption(("closure-owed",), False)},
        outcomes=lambda step: {"no-task"},
    )
}


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _refused(tmp_path: Path, text: str) -> str:
    path = _write(tmp_path, text)
    with pytest.raises(ScenarioRefusal) as info:
        load_scenario(path, KINDS)
    first = str(info.value).splitlines()[0]
    assert str(path) in first
    return first


def test_valid_file_loads_in_order(tmp_path):
    path = _write(
        tmp_path,
        "# the flow\nsteps:\n  - walk: implement\n  - walk: test\n",
    )
    assert load_scenario(path, KINDS) == Scenario(
        (Step("walk", "implement"), Step("walk", "test"))
    )


def test_duplicate_top_level_key(tmp_path):
    first = _refused(
        tmp_path, "steps:\n  - walk: test\nsteps:\n  - walk: test\n"
    )
    assert "steps" in first
    assert "line 3" in first


def test_duplicate_key_inside_a_step(tmp_path):
    first = _refused(
        tmp_path, "steps:\n  - walk: implement\n    walk: test\n"
    )
    assert "walk" in first
    assert "line 3" in first


def test_unknown_top_level_key(tmp_path):
    first = _refused(tmp_path, "steps:\n  - walk: test\nextra: 1\n")
    assert "extra" in first


def test_unknown_step_key(tmp_path):
    first = _refused(tmp_path, "steps:\n  - run: test\n")
    assert "steps[0].run: not a kind key" in first


@pytest.mark.parametrize("word", ["on", "no"])
def test_boolean_looking_values_stay_strings(tmp_path, word):
    first = _refused(tmp_path, f"steps:\n  - walk: {word}\n")
    assert f"'{word}'" in first


@pytest.mark.parametrize("value", ["true", "1"])
def test_wrong_type_is_refused(tmp_path, value):
    first = _refused(tmp_path, f"steps:\n  - walk: {value}\n")
    assert "steps[0].walk" in first
    assert "string is required" in first


@pytest.mark.parametrize(
    "text",
    ["", "# only a comment\n", "null\n", "steps: []\n---\nsteps: []\n", "steps: [\n"],
    ids=["empty", "comments", "null", "two-documents", "syntax-error"],
)
def test_not_one_document(tmp_path, text):
    assert "not one YAML document" in _refused(tmp_path, text)


@pytest.mark.parametrize(
    ("text", "concern"),
    [
        ("- walk: test\n", "top level"),
        ("{}\n", "steps"),
        ("steps:\n", "steps"),
        ("steps: []\n", "steps"),
        ("steps:\n  - implement\n", "steps[0]"),
        ("steps:\n  - walk: test\n    other: test\n", "steps[0].other: not read by 'walk'"),
    ],
    ids=[
        "list-top-level",
        "missing-steps",
        "null-steps",
        "empty-steps",
        "step-not-mapping",
        "step-two-keys",
    ],
)
def test_bad_shape(tmp_path, text, concern):
    assert concern in _refused(tmp_path, text)


def test_cycle_loads_with_option(tmp_path):
    path = _write(
        tmp_path,
        "steps:\n  - cycle:\n      until: no-task\n      steps:\n"
        "        - {walk: implement, stop: closure-owed}\n        - {walk: test}\n",
    )
    assert load_scenario(path, KINDS) == Scenario(
        (
            Cycle(
                "no-task",
                (
                    Step("walk", "implement", {"stop": "closure-owed"}),
                    Step("walk", "test"),
                ),
            ),
        )
    )


@pytest.mark.parametrize(
    ("text", "where"),
    [
        (
            "steps:\n  - cycle: {until: closure-owed, steps: [{walk: test}]}\n",
            "steps[0].cycle.until",
        ),
        (
            "steps:\n  - cycle: {until: no-task, steps: [{cycle: {until: no-task, steps: [{walk: test}]}}]}\n",
            "steps[0].cycle.steps[0].cycle",
        ),
        ("steps:\n  - {walk: implement, stop: other}\n", "steps[0].stop"),
        ("steps:\n  - {walk: implement, peer: x}\n", "steps[0].peer"),
        # A cycle with no step to return its `until` repeats with no signal,
        # and the runner of a cycle does not guard against it: the loader's
        # refusal is the only guard.
        ("steps:\n  - cycle: {until: no-task, steps: []}\n", "steps[0].cycle.steps: empty"),
    ],
    ids=["until-returned-by-none", "nested-cycle", "stop-value", "unread-key", "empty-cycle-steps"],
)
def test_refusals_name_the_key(tmp_path, text, where):
    assert where in _refused(tmp_path, text)


def test_until_is_checked_against_the_walks_own_outcomes(tmp_path):
    path = _write(
        tmp_path,
        "steps:\n  - cycle: {until: closure-owed, steps: [{walk: test, stop: closure-owed}]}\n",
    )
    with pytest.raises(ScenarioRefusal) as info:
        load_scenario(path, _SCENARIO_KINDS)
    first = str(info.value).splitlines()[0]
    assert "steps[0].cycle.until" in first


def test_until_closure_owed_loads_over_the_implement_walk(tmp_path):
    path = _write(
        tmp_path,
        "steps:\n  - cycle: {until: closure-owed, steps: [{walk: implement, stop: closure-owed}]}\n",
    )
    assert load_scenario(path, _SCENARIO_KINDS).steps == (
        Cycle("closure-owed", (Step("walk", "implement", {"stop": "closure-owed"}),)),
    )

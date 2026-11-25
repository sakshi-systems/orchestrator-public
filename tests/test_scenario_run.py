from __future__ import annotations

import pytest

from orchestrator.scenario_file import Cycle, Scenario, Step
from orchestrator.scenario_run import run_scenario


class StepFailed(Exception):
    pass


def test_steps_run_in_order_once_with_their_values():
    calls: list[tuple[str, str]] = []

    def record(name: str):
        def run(value, options):
            calls.append((name, value))
            return "ok"

        return run

    kinds = {"a": record("a"), "b": record("b")}
    scenario = Scenario(steps=(Step("a", "x"), Step("b", "y"), Step("a", "z")))

    run_scenario(scenario, kinds)

    assert calls == [("a", "x"), ("b", "y"), ("a", "z")]


def test_step_options_reach_the_callable_as_given():
    seen: list[object] = []

    def run(value, options):
        seen.append((value, options))
        return "ok"

    scenario = Scenario(steps=(Step("a", "x", {"stop": "closure-owed"}), Step("a", "y")))

    run_scenario(scenario, {"a": run})

    assert seen == [("x", {"stop": "closure-owed"}), ("y", {})]


def test_raising_step_stops_the_scenario():
    calls: list[tuple[str, str]] = []
    failure = StepFailed("second step")

    def ok(value, options):
        calls.append(("a", value))
        return "ok"

    def fail(value, options):
        calls.append(("b", value))
        raise failure

    scenario = Scenario(steps=(Step("a", "x"), Step("b", "y"), Step("a", "z")))

    with pytest.raises(StepFailed) as raised:
        run_scenario(scenario, {"a": ok, "b": fail})

    assert raised.value is failure
    assert calls == [("a", "x"), ("b", "y")]


def test_cycle_repeats_its_steps_until_one_returns_until_then_the_scenario_goes_on():
    calls: list[str] = []
    b_passes = 0

    def a(value, options):
        calls.append(value)
        return "again"

    def b(value, options):
        nonlocal b_passes
        b_passes += 1
        calls.append(value)
        return "stop" if b_passes == 2 else "again"

    scenario = Scenario(
        steps=(Cycle("stop", (Step("a", "a"), Step("b", "b"))), Step("a", "after"))
    )

    run_scenario(scenario, {"a": a, "b": b})

    assert calls == ["a", "b", "a", "b", "after"]


def test_cycle_ends_at_the_first_member_returning_until_and_skips_the_rest_of_the_pass():
    calls: list[str] = []

    def first(value, options):
        calls.append(value)
        return "stop"

    def later(value, options):
        calls.append(value)
        return "ok"

    scenario = Scenario(steps=(Cycle("stop", (Step("f", "first"), Step("l", "later"))),))

    run_scenario(scenario, {"f": first, "l": later})

    assert calls == ["first"]


def test_an_outcome_no_cycle_names_lets_a_plain_step_go_on():
    calls: list[str] = []

    def run(value, options):
        calls.append(value)
        return "closure-owed"

    scenario = Scenario(steps=(Step("a", "x"), Step("a", "y")))

    run_scenario(scenario, {"a": run})

    assert calls == ["x", "y"]


def test_exception_inside_a_cycle_ends_the_scenario():
    calls: list[str] = []
    failure = StepFailed("in the cycle")

    def ok(value, options):
        calls.append(value)
        return "again"

    def fail(value, options):
        calls.append(value)
        raise failure

    scenario = Scenario(
        steps=(Cycle("stop", (Step("a", "a"), Step("b", "b"))), Step("a", "after"))
    )

    with pytest.raises(StepFailed) as raised:
        run_scenario(scenario, {"a": ok, "b": fail})

    assert raised.value is failure
    assert calls == ["a", "b"]

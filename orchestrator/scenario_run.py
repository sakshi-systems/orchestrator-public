"""Running a loaded scenario's steps in order.

Steps run in the file's order, each through the callable the caller hands for
its kind, with the step's value and options; the callable returns the step's
outcome. A cycle runs its steps again from the first and ends at the first step
that returns its `until`, before the later steps of that pass; the scenario
then goes on. The module knows a step only by the outcome it returns, knows
nothing of what a step does and keeps no state: a step that raises ends the
scenario with its exception. The behaviour is docs/features/scenario-engine.md.
"""

from __future__ import annotations

from collections.abc import Mapping

from .ports import StepKindProtocol
from .scenario_file import Cycle, Scenario, Step


def _run_step(step: Step, kinds: Mapping[str, StepKindProtocol]) -> str:
    """Run one step through its kind's callable and return its outcome."""
    return kinds[step.kind](step.value, step.options)


def _run_cycle(cycle: Cycle, kinds: Mapping[str, StepKindProtocol]) -> None:
    """Repeat the cycle's steps until one returns the cycle's `until`."""
    while True:
        for step in cycle.steps:
            if _run_step(step, kinds) == cycle.until:
                return


def run_scenario(scenario: Scenario, kinds: Mapping[str, StepKindProtocol]) -> None:
    """Run each item of `scenario` in order: a step once, a cycle until it ends."""
    for item in scenario.steps:
        if isinstance(item, Cycle):
            _run_cycle(item, kinds)
        else:
            _run_step(item, kinds)

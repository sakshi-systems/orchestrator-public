"""Field-level role settings resolution without native runtime dependencies."""

from copy import deepcopy

import pytest

from orchestrator.ports import AgentRole, RoleSetting, resolve_role_settings


@pytest.fixture
def defaults():
    return {
        AgentRole.IMPLEMENTER: RoleSetting("implement-model", "implement-effort"),
        AgentRole.PLAN_REVIEWER: RoleSetting("review-model", "review-effort"),
        AgentRole.PLANNER_REVIEWER: RoleSetting("plan-model", "plan-effort"),
    }


@pytest.mark.parametrize("agents", [None, {}, {"plan_reviewer": {}}])
def test_resolve_role_settings_keeps_defaults_for_absent_fields(defaults, agents):
    assert resolve_role_settings(agents, defaults) == defaults


@pytest.mark.parametrize("role", list(AgentRole))
@pytest.mark.parametrize(
    "override",
    [
        {"model": " custom-model "},
        {"effort": " custom-effort "},
        {"model": " custom-model ", "effort": " custom-effort "},
    ],
)
def test_resolve_role_settings_overrides_fields_independently(defaults, role, override):
    expected = dict(defaults)
    expected[role] = RoleSetting(
        override.get("model", defaults[role].model),
        override.get("effort", defaults[role].effort),
    )

    assert resolve_role_settings({role.value: override}, defaults) == expected


def test_resolve_role_settings_returns_enum_order_independent_of_inputs(defaults):
    agents = {
        "plan_reviewer": {"model": "custom-review"},
        "implementer": {"effort": "custom-implement"},
        "planner_reviewer": {"model": "custom-plan", "effort": "custom-plan-effort"},
    }

    resolved = resolve_role_settings(agents, defaults)

    assert list(resolved) == list(AgentRole)
    assert resolved == {
        AgentRole.PLANNER_REVIEWER: RoleSetting("custom-plan", "custom-plan-effort"),
        AgentRole.PLAN_REVIEWER: RoleSetting("custom-review", "review-effort"),
        AgentRole.IMPLEMENTER: RoleSetting("implement-model", "custom-implement"),
    }


def test_resolve_role_settings_preserves_inputs_and_returns_independent_results(defaults):
    agents = {"implementer": {"model": "custom"}, "plan_reviewer": {}}
    original_agents = deepcopy(agents)
    original_defaults = dict(defaults)
    expected = {**defaults, AgentRole.IMPLEMENTER: RoleSetting("custom", "implement-effort")}

    first = resolve_role_settings(agents, defaults)
    second = resolve_role_settings(agents, defaults)

    assert agents == original_agents
    assert defaults == original_defaults
    assert first == second == expected
    assert first is not second

    first[AgentRole.IMPLEMENTER] = RoleSetting("other", "other")
    assert second == expected
    agents["implementer"]["model"] = "later"
    defaults[AgentRole.PLAN_REVIEWER] = RoleSetting("later", "later")
    assert second == expected

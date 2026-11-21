"""The role-routed backend: each role's call reaches the backend of that role's runtime."""

import pytest

from orchestrator.config import role_runtimes
from orchestrator.ports import AgentRole, RoleSetting
from orchestrator.role_backend import RoleRoutedBackend
from tests.fakes import FakeAgentBackend


def _routed():
    claude = FakeAgentBackend()
    codex = FakeAgentBackend(sid="codex-sid")
    runtimes = role_runtimes("claude", {"implementer": {"runtime": "codex"}})
    return RoleRoutedBackend({"claude": claude, "codex": codex}, runtimes), claude, codex


def test_each_role_runs_on_the_backend_of_its_runtime():
    backend, claude, codex = _routed()

    returns = {
        role: backend.run(role=role, prompt=f"p-{role.value}", cwd="c", instructions="i", session_id="sid-in")
        for role in AgentRole
    }

    assert [call.role for call in claude.calls] == [
        AgentRole.PLANNER, AgentRole.PLAN_REVIEWER, AgentRole.CODE_REVIEWER,
    ]
    assert [call.role for call in codex.calls] == [AgentRole.IMPLEMENTER]
    for call in [*claude.calls, *codex.calls]:
        assert call.prompt == f"p-{call.role.value}"
        assert call.cwd == "c" and call.instructions == "i" and call.session_id == "sid-in"
    assert returns[AgentRole.PLANNER] == ("fake output", "planner-sid-1")
    assert returns[AgentRole.IMPLEMENTER] == ("fake output", "codex-sid")


def test_role_settings_come_from_each_roles_backend_in_role_order():
    backend, claude, codex = _routed()
    codex.role_settings = {
        **codex.role_settings,
        AgentRole.IMPLEMENTER: RoleSetting("codex-model", "codex-effort"),
    }

    settings = backend.role_settings

    assert list(settings) == list(AgentRole)
    assert settings[AgentRole.IMPLEMENTER] == RoleSetting("codex-model", "codex-effort")
    assert all(
        settings[role] == claude.role_settings[role]
        for role in AgentRole if role is not AgentRole.IMPLEMENTER
    )


def test_runtime_of_names_each_roles_effective_runtime():
    backend, _, _ = _routed()

    assert [backend.runtime_of(role) for role in AgentRole] == [
        "claude", "claude", "codex", "claude",
    ]
    assert backend.runtimes == role_runtimes("claude", {"implementer": {"runtime": "codex"}})


def test_a_role_of_an_unheld_runtime_is_a_loud_error():
    runtimes = role_runtimes("claude")

    with pytest.raises(KeyError):
        RoleRoutedBackend({}, runtimes).role_settings


def test_a_hermes_role_routes_to_its_instance_backend():
    local = FakeAgentBackend()
    main = FakeAgentBackend(sid="main-sid")
    second = FakeAgentBackend(sid="second-sid")
    runtimes = role_runtimes("claude", {
        "planner": {"runtime": "hermes", "instance": "main"},
        "code_reviewer": {"runtime": "hermes", "instance": "second"},
    })
    backend = RoleRoutedBackend(
        {"claude": local, "hermes:main": main, "hermes:second": second}, runtimes,
    )

    returns = {
        role: backend.run(role=role, prompt="p", cwd="c", instructions="i")
        for role in AgentRole
    }

    assert [call.role for call in local.calls] == [AgentRole.PLAN_REVIEWER, AgentRole.IMPLEMENTER]
    assert [call.role for call in main.calls] == [AgentRole.PLANNER]
    assert [call.role for call in second.calls] == [AgentRole.CODE_REVIEWER]
    assert returns[AgentRole.PLANNER][1] == "main-sid"
    assert returns[AgentRole.CODE_REVIEWER][1] == "second-sid"
    assert [backend.runtime_of(role) for role in AgentRole] == [
        "hermes", "claude", "claude", "hermes",
    ]


def test_a_hermes_role_carries_no_role_settings():
    local = FakeAgentBackend()
    main = FakeAgentBackend(sid="main-sid")
    runtimes = role_runtimes("claude", {"planner": {"runtime": "hermes", "instance": "main"}})
    backend = RoleRoutedBackend({"claude": local, "hermes:main": main}, runtimes)

    settings = backend.role_settings

    assert list(settings) == [AgentRole.PLAN_REVIEWER, AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER]
    assert all(settings[role] == local.role_settings[role] for role in settings)


def test_the_routed_backend_forwards_the_exchange_and_the_key_recipe_names_a_head():
    from orchestrator.role_backend import RoleRoutedBackend, backend_key

    class Recording:
        def __init__(self):
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return "answer", "sid"

    from orchestrator.ports import RoleSetting

    class Local:
        role_settings = {role: RoleSetting("model", "effort") for role in AgentRole}

        def run(self, **kwargs):
            raise AssertionError("the local backend is not this call's")

    head = {"runtime": "head", "head": {"repository": "/repos/p", "owner": "max", "folder": "r-d"}}
    recording = Recording()
    head_key = backend_key(head)
    routed = RoleRoutedBackend(
        {head_key: recording, "claude": Local(), "codex": Local()},
        {"code_reviewer": head, "planner": {"runtime": "claude"},
         "plan_reviewer": {"runtime": "claude"}, "implementer": {"runtime": "codex"}},
    )

    import json as _json
    assert backend_key({"runtime": "claude"}) == "claude"
    assert backend_key({"runtime": "codex"}) == "codex"
    assert backend_key({"runtime": "hermes", "instance": "main"}) == "hermes:main"
    assert backend_key(head) == "head:" + _json.dumps(["/repos/p", "max", "r-d"])

    answer, sid = routed.run(role=AgentRole.CODE_REVIEWER, prompt="p", cwd="c",
                             instructions="i", exchange="reviews/01-slug-review-1")

    assert (answer, sid) == ("answer", "sid")
    assert recording.calls[0]["exchange"] == "reviews/01-slug-review-1"
    assert AgentRole.CODE_REVIEWER not in routed.role_settings  # a head role carries no settings
    assert AgentRole.PLANNER in routed.role_settings


def test_two_heads_whose_values_collide_under_a_separator_give_two_keys():
    import json
    from orchestrator.role_backend import backend_key

    def head(repository, owner, folder):
        return {"runtime": "head", "head": {"repository": repository, "owner": owner, "folder": folder}}

    one = head("/a:x", "y", "z")
    other = head("/a", "x:y", "z")
    assert backend_key(one) != backend_key(other)
    assert backend_key(one) == "head:" + json.dumps(["/a:x", "y", "z"])
    assert backend_key(other) == "head:" + json.dumps(["/a", "x:y", "z"])

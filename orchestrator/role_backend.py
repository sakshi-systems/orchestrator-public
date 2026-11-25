"""The role-routed backend: one object that answers each role through the backend of that role's runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol, cast

from .ports import AgentBackendProtocol, AgentRole, RoleSetting


def backend_key(entry: Mapping[str, object]) -> str:
    """The one recipe for a backend's key: a local runtime by its name, a
    Hermes instance under `hermes:<instance>`, and a head under `head:` and
    the JSON array of its three values, so a value holding the separator
    cannot collide with another head's key."""
    runtime = entry["runtime"]
    if runtime == "hermes":
        return f"hermes:{entry['instance']}"
    if runtime == "head":
        head = entry["head"]
        return "head:" + json.dumps([head["repository"], head["owner"], head["folder"]])
    return str(runtime)


class _RoleSettingsBackend(AgentBackendProtocol, Protocol):
    """A backend of a local runtime: the invocation port plus its resolved role settings."""

    @property
    def role_settings(self) -> Mapping[AgentRole, RoleSetting]: ...


class RoleRoutedBackend:
    """Implements the agent-runtime port by dispatching on the role.

    Holds one backend per runtime the run's roles take — a local runtime by its
    name, a Hermes instance under `hermes:<instance>` — with the effective
    runtime record keyed by role; a role's call, and its resolved model and
    effort, reach the backend of that role's runtime, and a runtime no role
    takes is held by no one.
    """

    def __init__(
        self,
        backends: Mapping[str, AgentBackendProtocol],
        runtimes: Mapping[str, Mapping[str, str]],
    ) -> None:
        self._backends = dict(backends)
        self._runtimes = MappingProxyType(dict(runtimes))

    @property
    def runtimes(self) -> Mapping[str, Mapping[str, str]]:
        """The effective runtime of each role, keyed by role, as the record the sidecar holds."""
        return self._runtimes

    def runtime_of(self, role: AgentRole) -> str:
        """The name of the runtime the effective record gives `role`."""
        return self._runtimes[role.value]["runtime"]

    def _backend_of(self, role: AgentRole) -> AgentBackendProtocol:
        return self._backends[backend_key(self._runtimes[role.value])]

    @property
    def role_settings(self) -> Mapping[AgentRole, RoleSetting]:
        """Each local-runtime role's resolved settings, from the backend of that
        role's runtime, over the roles the record holds, in the record's order;
        a role on Hermes or a head carries none."""
        settings: dict[AgentRole, RoleSetting] = {}
        for name in self._runtimes:
            role = AgentRole(name)
            if self._runtimes[name]["runtime"] in ("hermes", "head"):
                continue
            settings[role] = cast(_RoleSettingsBackend, self._backend_of(role)).role_settings[role]
        return MappingProxyType(settings)

    def run(
        self,
        *,
        role: AgentRole,
        prompt: str,
        cwd: str,
        instructions: str,
        session_id: str | None = None,
        exchange: str | None = None,
        bases: tuple[str, ...] | None = None,
    ) -> tuple[str, str]:
        return self._backend_of(role).run(
            role=role,
            prompt=prompt,
            cwd=cwd,
            instructions=instructions,
            session_id=session_id,
            exchange=exchange,
            bases=bases,
        )

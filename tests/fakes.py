"""Test doubles for the agent-runtime port: no LLM and no native CLI ever runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from orchestrator.ports import AgentRole, RoleSetting


@dataclass
class BackendCall:
    role: AgentRole
    prompt: str
    cwd: str
    instructions: str
    session_id: str | None
    exchange: str | None = None
    bases: tuple[str, ...] | None = None


class FakeAgentBackend:
    """Records every call, runs `on_call`, and returns identities.

    Without `sid` each call returns a distinct identity; with it, every call returns `sid`.
    The answer is what `on_call` returns — the string when it returns one, `"fake output"`
    otherwise. `returned` lists exactly the identities handed back.
    """

    def __init__(self, on_call: Callable[[BackendCall], str | None] | None = None, sid: str | None = None) -> None:
        self.on_call = on_call
        self.sid = sid
        self.calls: list[BackendCall] = []
        self.returned: list[str] = []
        self.role_settings = {
            role: RoleSetting(f"fake-{role.value}", "fake-effort") for role in AgentRole
        }

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
        call = BackendCall(role, prompt, cwd, instructions, session_id, exchange, bases)
        self.calls.append(call)
        new_sid = self.sid or f"{role.value}-sid-{len(self.calls)}"
        self.returned.append(new_sid)
        result = self.on_call(call) if self.on_call is not None else None
        answer = result if isinstance(result, str) else "fake output"
        return answer, new_sid

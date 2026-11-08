"""Test doubles for the agent-runtime port: no LLM and no native CLI ever runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.ports import AgentRole, RoleSetting


@dataclass
class BackendCall:
    role: AgentRole
    prompt: str
    cwd: str
    instructions: str
    session_id: str | None


class FakeAgentBackend:
    """Records every call, optionally runs `on_call` (to write artifacts), and returns identities.

    Without `sid` each call returns a distinct identity; with it, every call returns `sid`.
    `returned` lists exactly what was handed back.
    """

    def __init__(self, on_call: Callable[[BackendCall], None] | None = None, sid: str | None = None) -> None:
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
    ) -> tuple[str, str]:
        call = BackendCall(role, prompt, cwd, instructions, session_id)
        self.calls.append(call)
        new_sid = self.sid or f"{role.value}-sid-{len(self.calls)}"
        self.returned.append(new_sid)
        if self.on_call is not None:
            self.on_call(call)
        return "fake output", new_sid


def writing_backend(write_path: Path, content: str, sid: str = "sid-1") -> FakeAgentBackend:
    """A fake whose every call writes `content` to `write_path` and returns `sid`."""
    return FakeAgentBackend(on_call=lambda call: write_path.write_text(content), sid=sid)


def silent_backend(sid: str = "sid-1") -> FakeAgentBackend:
    """A fake that writes nothing and returns `sid`."""
    return FakeAgentBackend(sid=sid)


def target_path(prompt: str) -> Path:
    """The artifact path named by the prompt's `... to: <path>` line."""
    match = re.search(r"to: (\S+)", prompt)
    assert match, prompt
    return Path(match.group(1))

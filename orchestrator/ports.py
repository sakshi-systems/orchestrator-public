"""Ports and kinds: the agent-runtime port with its role identity, and the verify port with the verify-kind type."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Callable, NamedTuple, Protocol


class AgentRole(Enum):
    """The semantic role identity handed to the runtime, independent of instruction text."""

    PLANNER_REVIEWER = "planner_reviewer"
    PLAN_REVIEWER = "plan_reviewer"
    IMPLEMENTER = "implementer"


class RoleSetting(NamedTuple):
    """The model and effort resolved for one semantic role."""

    model: str
    effort: str


def resolve_role_settings(
    agents: Mapping[str, Mapping[str, str]] | None,
    defaults: Mapping[AgentRole, RoleSetting],
) -> dict[AgentRole, RoleSetting]:
    """Resolve an already validated block field by field, in role order."""
    block = agents if agents is not None else {}
    resolved = {}
    for role in AgentRole:
        settings = block.get(role.value, {})
        default = defaults[role]
        resolved[role] = RoleSetting(
            model=settings.get("model", default.model),
            effort=settings.get("effort", default.effort),
        )
    return resolved


class AgentBackendProtocol(Protocol):
    """Carry out the role's turn in `cwd` and return (invocation output, session identity).

    Invocation faults propagate. Instructions are complete on every call; the
    backend decides whether a continuation needs them. No native tool names or
    model/effort parameters cross this boundary.
    """

    def run(
        self,
        *,
        role: AgentRole,
        prompt: str,
        cwd: str,
        instructions: str,
        session_id: str | None = None,
    ) -> tuple[str, str]: ...


class VerifierProtocol(Protocol):
    """Given the plan and output path, write the verify artifact and report pass or fail by its completion signal."""

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None) -> bool: ...


class VerifyKind(NamedTuple):
    """Everything that depends on the verify choice (review vs. test run)."""
    step: str
    fail_tag: str
    output_dirname: str
    output_suffix: str  # artifact tail with an {n} placeholder, e.g. "-review-{n}.md"
    pass_signal: str
    running_header: str
    pass_line_label: str
    fail_line_label: str
    max_iterations_message: str  # template with {n}, {path}, {content} placeholders
    make_verifier: Callable[[VerifierProtocol, Path], VerifierProtocol]  # (reviewer, project_dir) -> verifier

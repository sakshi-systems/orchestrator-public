"""Ports and kinds: the agent-runtime port with its role identity, and the verify port with the verify-kind type."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Callable, NamedTuple, Protocol


class AgentRole(Enum):
    """The semantic role identity handed to the runtime, independent of instruction text."""

    PLANNER = "planner"
    PLAN_REVIEWER = "plan_reviewer"
    IMPLEMENTER = "implementer"
    CODE_REVIEWER = "code_reviewer"


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
        exchange: str | None = None,
    ) -> tuple[str, str]: ...


class IterationState(NamedTuple):
    """The iteration's state as a commit on the run's ref: the base commit the
    task began on, the iteration's commit and the ref's full name."""

    base: str
    commit: str
    ref: str


class VerifierProtocol(Protocol):
    """Given the plan and output path, write the verify artifact and report pass or fail by its completion signal."""

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None, implementer_answer_path: Path | None, iteration_state: IterationState) -> bool: ...


class ImplementerResultProtocol(Protocol):
    """The port by which an implementer's result reaches the run's tree."""

    def prepare(self, plan_path: Path) -> None:
        """Prepare a task's first implementer call, made when no implementer
        session is recorded."""
        ...

    def channel_text(self, plan_path: Path, *, first: bool) -> str:
        """The channel text appended to the implementer's assignment; empty
        when the implementer works in the run's tree."""
        ...

    def lay(self, plan_path: Path) -> None:
        """Lay the implementer's result as the run's working tree, called after
        the answer is written and not escalated."""
        ...


class VerifyKind(NamedTuple):
    """Everything that depends on the verify choice (review vs. test run)."""
    step: str
    fail_tag: str
    output_dirname: str
    output_suffix: str  # artifact tail with an {n} placeholder, e.g. "-review-{n}.md"
    pass_signal: str
    running_header: str
    pass_line: str  # template with a {path} placeholder
    fail_line: str  # template with a {path} placeholder
    max_iterations_message: str  # template with {n}, {path}, {content} placeholders
    make_verifier: Callable[[VerifierProtocol, Path], VerifierProtocol]  # (reviewer, project_dir) -> verifier

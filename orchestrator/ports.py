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
    model/effort parameters cross this boundary. `bases` is the task's
    recorded base commit of each repository of the run's tree, in the tree's
    order, or `None` where the task's record holds none.
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
        bases: tuple[str, ...] | None = None,
    ) -> tuple[str, str]: ...


class Repository(NamedTuple):
    """One repository of the run's tree: where the run's git calls for it
    run, and the folder of it the run takes where the repository is not the
    project's own — `None` there, and the `git rev-parse --show-prefix`
    text of `.ai-factory`, trailing slash included and empty where
    `.ai-factory` is a repository of its own, for the other.

    Everything a consumer needs of the difference is yielded here, so no
    consumer branches on `folder`."""

    directory: Path
    folder: str | None

    @property
    def label(self) -> str:
        """The text a message names the repository by: the project's by that
        name, the other by holding `.ai-factory` — the folder it resolves to
        named, and where that folder is the repository's own root, the root
        named."""
        if self.folder is None:
            return "the project's repository"
        if not self.folder:
            return "the repository holding `.ai-factory`, which is its root"
        return f"the repository holding `.ai-factory`, its folder `{self.folder}`"

    @property
    def stage(self) -> tuple[str, ...]:
        """The arguments of the call that stages the repository: everything
        for the project's own, and — the other repository's index being
        shared with the other projects it holds — only the folder
        `.ai-factory` resolves to."""
        if self.folder is None:
            return ("add", "-A")
        return ("add", "-A", ".")

    @property
    def scope(self) -> tuple[str, ...]:
        """The pathspec a check of the index and a commit over it take: the
        whole index for the project's repository, and only the folder for
        the other, whose index holds other projects' staged changes too."""
        if self.folder is None:
            return ()
        return ("--", ".")

    def named(self, name: str) -> str:
        """The task's name in the repository: `name` in the project's own,
        and — one repository holding several projects' folders — the folder
        before it, so two projects never share a name."""
        if self.folder is None:
            return name
        return f"{self.folder}{name}"


class IterationState(NamedTuple):
    """The iteration's state as a commit on the task's ref in one repository
    of the run's tree: the repository it was made in, the base commit the
    task began on there, the iteration's commit and the ref's full name in
    that repository."""

    repository: Repository
    base: str
    commit: str
    ref: str


class PlannerProtocol(Protocol):
    """The plan stage's port: write the task's plan file at `plan_path`.

    `session_id` is `None` where the implementation holds none.
    """

    session_id: str | None

    def plan(self, task_title: str, task_description: str, plan_path: Path, plan_review_path: Path | None = None, roadmap_path: Path | None = None, line_number: int | None = None) -> None: ...


class VerifierProtocol(Protocol):
    """Given the plan and output path, write the verify artifact and report pass or fail by its completion signal."""

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None, implementer_answer_path: Path | None, iteration_states: tuple[IterationState, ...]) -> bool: ...


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

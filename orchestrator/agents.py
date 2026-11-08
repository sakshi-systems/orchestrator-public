"""Roles — the planner/reviewer, plan reviewer and implementer invoked through the agent-runtime port, and the shell test runner."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .errors import EscalationError, MissingArtifactError
from .ports import AgentBackendProtocol, AgentRole, VerifierProtocol, VerifyKind
from .protocol import PLAN_REVIEW_PASS, _escalation_excerpt, _has_escalation, _has_signal, _write_session

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


_PROMPT_SEPARATOR = "\n\n---\n\n"


def _compose_instructions(*parts: str) -> str:
    """Common execution instructions first, then the role's parts in the given order."""
    return _PROMPT_SEPARATOR.join((_load_prompt("execution"), *parts))


def _review_verifier(planner_reviewer: VerifierProtocol, project_dir: Path) -> VerifierProtocol:
    return planner_reviewer


def _test_run_verifier(planner_reviewer: VerifierProtocol, project_dir: Path) -> VerifierProtocol:
    return TestRunner(project_dir)


REVIEW_VERIFY = VerifyKind(
    step="review",
    fail_tag="review_failed:",
    output_dirname="reviews",
    output_suffix="-review-{n}.md",
    pass_signal="REVIEW_PASS",
    running_header="REVIEWING",
    pass_line_label="REVIEW PASSED",
    fail_line_label="Review found issues",
    max_iterations_message="Implement failed\n\nLast review: {path}\n\n{content}",
    make_verifier=_review_verifier,
)

TEST_RUN_VERIFY = VerifyKind(
    step="test_run",
    fail_tag="test_run_failed:",
    output_dirname="test-runs",
    output_suffix="-test-{n}.txt",
    pass_signal="TEST_PASS",
    running_header="RUNNING TESTS",
    pass_line_label="TESTS PASSED",
    fail_line_label="Tests failed",
    max_iterations_message="Test failed\n\nLast run: {path}\n\n{content}",
    make_verifier=_test_run_verifier,
)


class PlannerReviewer:
    """Plans and reviews tasks. Same session — reviewer has planner's context."""

    def __init__(
        self,
        project_dir: Path,
        backend: AgentBackendProtocol,
        planner_prompt_name: str = "planner",
    ):
        self.project_dir = project_dir
        self.backend = backend
        self.planner_prompt = _load_prompt(planner_prompt_name)
        self.reviewer_prompt = _load_prompt("reviewer")
        self.escalation_prompt = _load_prompt("escalation")
        self.system_prompt = _compose_instructions(self.planner_prompt, self.reviewer_prompt, self.escalation_prompt)
        self.session_id: str | None = None

    def plan(self, task_title: str, task_description: str, plan_path: Path, plan_review_path: Path | None = None, roadmap_path: Path | None = None, line_number: int | None = None) -> None:
        roadmap_line = ""
        if roadmap_path is not None:
            location = f" (line {line_number + 1})" if line_number is not None else ""
            roadmap_line = f"Roadmap: {roadmap_path}{location}\n"
        prompt = (
            f"Create or update an implementation plan for this task:\n\n"
            f"{roadmap_line}"
            f"**{task_title}**\n"
            f"{task_description}\n\n"
            f"Write the plan to: {plan_path}\n"
        )
        if plan_review_path is not None and plan_review_path.exists():
            prompt += (
                f"\nYour plan at {plan_path} was reviewed and has issues.\n\n"
                f"Read the review at: {plan_review_path}\n\n"
                f"Update the plan to address the issues in that review.\n"
            )

        _, self.session_id = self.backend.run(
            role=AgentRole.PLANNER_REVIEWER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=self.session_id,
        )
        _write_session(plan_path, "planner", self.session_id)

        if plan_path.exists() and _has_escalation(plan_path.read_text()):
            excerpt = _escalation_excerpt(plan_path.read_text())
            _write_session(plan_path, "escalation", f"planner: {excerpt} ({plan_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{plan_path}: {excerpt}")

    def review(self, plan_path: Path, review_path: Path, prev_review_path: Path | None = None) -> bool:
        """Review code changes. Uses same session as planner for deep context."""
        if prev_review_path:
            prompt = (
                f"This is a RE-REVIEW after fixes were applied to address the previous review at: {prev_review_path}\n"
                f"Do NOT trust your session memory about file contents — the code has changed since your last pass.\n"
                f"For each finding in the previous review, open the cited file as it is now and read it in full, "
                f"quote the CURRENT content relevant to that finding, and give a verdict: Fixed or Not fixed, "
                f"with the quote as evidence.\n\n"
                f"Then run the normal full review for new issues:\n"
                f"The plan for context is at: {plan_path}\n"
                f"Review the CODE CHANGES for bugs, security issues, and correctness problems.\n"
                f"Run `git diff HEAD` and `git status` to see ALL changes.\n"
                f"Read each changed/new file IN FULL — understand the surrounding code, not just the diff.\n"
                f"Think about what will break at runtime: missing migrations, type mismatches, race conditions, etc.\n"
                f"Write your full review (per-finding verdicts plus any new issues) to: {review_path}\n"
                f"If you have no findings at all, end the review file with {REVIEW_VERIFY.pass_signal} on its own line.\n"
            )
        else:
            prompt = (
                f"The plan for context is at: {plan_path}\n"
                f"Review the CODE CHANGES for bugs, security issues, and correctness problems.\n"
                f"Run `git diff HEAD` and `git status` to see ALL changes.\n"
                f"Read each changed/new file IN FULL — understand the surrounding code, not just the diff.\n"
                f"Think about what will break at runtime: missing migrations, type mismatches, race conditions, etc.\n"
                f"Write your full review to: {review_path}\n"
                f"If you have no findings at all, end the review file with {REVIEW_VERIFY.pass_signal} on its own line.\n"
            )

        output, self.session_id = self.backend.run(
            role=AgentRole.PLANNER_REVIEWER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=self.session_id,
        )
        _write_session(plan_path, "planner", self.session_id)

        if not review_path.exists():
            raise MissingArtifactError(
                f"Agent ended without writing its review\n{review_path}"
            )

        # Check the review file, not the chat output — look for the review kind's pass signal on its own line
        review_text = review_path.read_text()
        if _has_escalation(review_text):
            excerpt = _escalation_excerpt(review_text)
            _write_session(plan_path, "escalation", f"reviewer: {excerpt} ({review_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{review_path}: {excerpt}")

        return _has_signal(review_text, REVIEW_VERIFY.pass_signal)

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None) -> bool:
        return self.review(plan_path, out_path, prev_review_path=prev_out_path)


class PlanReviewer:
    """Reviews a plan before implementation. Fresh session — no planner bias."""

    def __init__(self, project_dir: Path, backend: AgentBackendProtocol):
        self.project_dir = project_dir
        self.backend = backend
        self.system_prompt = _compose_instructions(_load_prompt("reviewer"), _load_prompt("escalation"))

    def review_plan(self, plan_path: Path, review_path: Path, attempt: int) -> bool:
        """Review a plan file. Writes review to review_path. Returns True if passed.

        The reviewer's session id is written to the plan's sidecar under
        ``plan-reviewer-<attempt>`` as soon as the agent returns, before the
        review file is checked, so a reviewer that wrote no review or escalated
        still leaves its id.

        Raises MissingArtifactError if the agent ends its turn without writing
        the review file — a halt, not a failed review.
        """
        prompt = (
            f"Review the PLAN at: {plan_path}\n"
            f"Read the plan file and the codebase it targets.\n"
            f"Check for: missing steps, wrong assumptions about the codebase, "
            f"architectural mistakes, missing migrations, security issues, "
            f"incorrect file paths or API usage.\n"
            f"Write your full review to: {review_path}\n"
            f"If the plan is solid, end the review file with {PLAN_REVIEW_PASS} on its own line.\n"
        )

        _, sid = self.backend.run(
            role=AgentRole.PLAN_REVIEWER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=None,
        )
        _write_session(plan_path, f"plan-reviewer-{attempt}", sid)
        if not review_path.exists():
            raise MissingArtifactError(
                f"Agent ended without writing its review\n{review_path}"
            )

        review_text = review_path.read_text()
        if _has_escalation(review_text):
            excerpt = _escalation_excerpt(review_text)
            _write_session(plan_path, "escalation", f"plan-reviewer: {excerpt} ({review_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{review_path}: {excerpt}")
        return _has_signal(review_text, PLAN_REVIEW_PASS)


class Implementer:
    """Implements the plan, then applies the review feedback for the current iteration. Same session."""

    def __init__(self, project_dir: Path, backend: AgentBackendProtocol):
        self.project_dir = project_dir
        self.backend = backend
        self.system_prompt = _compose_instructions(_load_prompt("implementer"), _load_prompt("escalation"))
        self.session_id: str | None = None

    def implement(self, plan_path: Path, feedback_path: Path | None = None, roadmap_path: Path | None = None, line_number: int | None = None) -> None:
        if self.session_id:
            # Continuing — apply the review feedback
            prompt = (
                f"Review feedback has been written to {feedback_path}. "
                f"Read it and apply the fixes."
            )
        else:
            roadmap_line = ""
            if roadmap_path is not None and line_number is not None:
                roadmap_line = f"Roadmap: {roadmap_path} (line {line_number + 1})\n"

            prompt = (
                f"{roadmap_line}"
                f"Implement the plan at: {plan_path}"
            )

        _, self.session_id = self.backend.run(
            role=AgentRole.IMPLEMENTER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=self.session_id,
        )
        _write_session(plan_path, "implementer", self.session_id)

        if plan_path.exists() and _has_escalation(plan_path.read_text()):
            excerpt = _escalation_excerpt(plan_path.read_text())
            _write_session(plan_path, "escalation", f"implementer: {excerpt} ({plan_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{plan_path}: {excerpt}")


class TestRunner:
    """Runs the test command from the plan file and captures output. No LLM."""

    __test__ = False  # not a pytest test class; stops pytest collecting it by name

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None) -> bool:
        """Extract test command from plan, run it, write output. Returns True if exit code 0."""
        cmd = self._extract_test_command(plan_path)
        if not cmd:
            out_path.write_text("ERROR: No '## Test Command' section found in plan.\n")
            return False

        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n--- TestRunner: {cmd} ---")
        start = time.monotonic()
        result = subprocess.run(
            cmd, shell=True, cwd=str(self.project_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        elapsed = time.monotonic() - start
        output = (
            f"$ {cmd}\n"
            f"exit code: {result.returncode}\n"
            f"elapsed: {elapsed:.1f}s\n\n"
            f"{result.stdout}"
        )
        passed = result.returncode == 0
        if passed:
            output += f"\n{TEST_RUN_VERIFY.pass_signal}"
        out_path.write_text(output)
        status = "PASSED" if passed else "FAILED"
        print(f"--- TestRunner: {status} (exit {result.returncode}, {elapsed:.1f}s) ---")
        return passed

    @staticmethod
    def _extract_test_command(plan_path: Path) -> str | None:
        """Read `## Test Command` section from the plan and return the command string."""
        lines = plan_path.read_text().splitlines()
        in_section = False
        for line in lines:
            if line.strip() == "## Test Command":
                in_section = True
                continue
            if in_section:
                stripped = line.strip()
                if stripped.startswith("#"):
                    break
                if stripped.startswith("`") and stripped.endswith("`"):
                    return stripped.strip("`")
                if stripped:
                    return stripped
        return None

"""Roles — the planner, code reviewer, plan reviewer and implementer invoked through the agent-runtime port, and the shell test runner."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .artifact_layout import REVIEWS_DIRNAME, REVIEW_SUFFIX, TEST_RUNS_DIRNAME, TEST_RUN_SUFFIX
from .errors import EscalationError, HaltError
from .ports import AgentBackendProtocol, AgentRole, ImplementerResultProtocol, IterationState, VerifierProtocol, VerifyKind
from .protocol import PLAN_REVIEW_PASS, _escalation_excerpt, _has_escalation, _has_signal, _read_artifact, _write_artifact, _write_session

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _exchange_of(project_dir: Path, artifact: Path) -> str:
    """The artifact's exchange: its path relative to .ai-factory, without its
    suffix and written with `/`, such as `reviews/john-doe/01-slug-review-1`."""
    return artifact.with_suffix("").relative_to(project_dir / ".ai-factory").as_posix()


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


_PROMPT_SEPARATOR = "\n\n---\n\n"


def _compose_instructions(*parts: str) -> str:
    """Common execution instructions first, then the role's parts in the given order."""
    return _PROMPT_SEPARATOR.join((_load_prompt("execution"), *parts))


def _review_verifier(code_reviewer: VerifierProtocol, project_dir: Path) -> VerifierProtocol:
    return code_reviewer


def _test_run_verifier(code_reviewer: VerifierProtocol, project_dir: Path) -> VerifierProtocol:
    return TestRunner(project_dir)


REVIEW_VERIFY = VerifyKind(
    step="review",
    fail_tag="review_failed:",
    output_dirname=REVIEWS_DIRNAME,
    output_suffix=REVIEW_SUFFIX,
    pass_signal="REVIEW_PASS",
    running_header="REVIEWING",
    pass_line="REVIEW PASSED — see {path}",
    fail_line="Review found issues — see {path}",
    max_iterations_message="Implement failed\n\nLast review: {path}\n\n{content}",
    make_verifier=_review_verifier,
)

TEST_RUN_VERIFY = VerifyKind(
    step="test_run",
    fail_tag="test_run_failed:",
    output_dirname=TEST_RUNS_DIRNAME,
    output_suffix=TEST_RUN_SUFFIX,
    pass_signal="TEST_PASS",
    running_header="RUNNING TESTS",
    pass_line="TESTS PASSED — see {path}",
    fail_line="Tests failed — see {path}",
    max_iterations_message="Test failed\n\nLast run: {path}\n\n{content}",
    make_verifier=_test_run_verifier,
)


class Planner:
    """Plans tasks in a session of its own, never reviewing code."""

    def __init__(
        self,
        project_dir: Path,
        backend: AgentBackendProtocol,
        planner_prompt_name: str = "planner",
    ) -> None:
        self.project_dir = project_dir
        self.backend = backend
        self.planner_prompt = _load_prompt(planner_prompt_name)
        self.escalation_prompt = _load_prompt("escalation")
        self.system_prompt = _compose_instructions(self.planner_prompt, self.escalation_prompt)
        self.session_id: str | None = None

    def plan(self, task_title: str, task_description: str, plan_path: Path, plan_review_path: Path | None = None, roadmap_path: Path | None = None, line_number: int | None = None) -> None:
        """Plan a task. The run writes the planner's answer to plan_path.

        The message carries the task and, on a revision, the current plan's and
        the review's text under their headings; it names no path of the run's
        tree other than the roadmap line. The planner's session id is written to
        the sidecar under ``planner`` as soon as the agent returns, before the
        answer is written. An empty answer halts with EmptyAnswerError, leaves
        the plan file as it was and leaves ``step`` at ``planning:<N>``, so a
        resumed run repeats the round.
        """
        roadmap_line = ""
        if roadmap_path is not None:
            location = f" (line {line_number + 1})" if line_number is not None else ""
            roadmap_line = f"Roadmap: {roadmap_path}{location}\n"
        prompt = (
            f"Create or update an implementation plan for this task:\n\n"
            f"{roadmap_line}"
            f"**{task_title}**\n"
            f"{task_description}\n\n"
            f"Return the plan as your answer.\n"
        )
        if plan_review_path is not None and plan_review_path.exists():
            prompt += (
                "\nYour plan was reviewed and has issues.\n\n"
                "Address the issues of the review below and return the revised plan as your answer.\n"
            )
            sections = []
            if plan_path.exists():
                sections += ["## Plan", "", _read_artifact(plan_path)]
            sections += ["## Plan review", "", _read_artifact(plan_review_path)]
            prompt += "\n" + "\n".join(sections)

        revision = plan_review_path if plan_review_path is not None and plan_review_path.exists() else None
        exchange = _exchange_of(self.project_dir, revision if revision is not None else plan_path)
        answer, self.session_id = self.backend.run(
            role=AgentRole.PLANNER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=self.session_id,
            exchange=exchange,
        )
        _write_session(plan_path, "planner", self.session_id)
        _write_artifact(plan_path, answer)

        if _has_escalation(answer):
            excerpt = _escalation_excerpt(answer)
            _write_session(plan_path, "escalation", f"planner: {excerpt} ({plan_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{plan_path}: {excerpt}")


_SPEC_TAG = re.compile(r"Spec: `([^`]+)`")


class SpecLayer:
    """Lay the task's spec as the plan — the plan stage without a planner.

    The spec is read from the project's `.ai-factory/` path without the path
    ever being resolved, since `.ai-factory` may be a link out of the project,
    and is laid unchanged under the plan's heading.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.session_id: str | None = None

    def plan(self, task_title: str, task_description: str, plan_path: Path, plan_review_path: Path | None = None, roadmap_path: Path | None = None, line_number: int | None = None) -> None:
        """Lay the spec the task's description names as the plan file."""
        tags = _SPEC_TAG.findall(task_description)
        if not tags:
            raise HaltError(
                f"The task '{task_title}' names no spec: its description carries no 'Spec:' tag"
            )
        relpath = tags[-1]
        if not relpath.startswith(".ai-factory/"):
            raise HaltError(
                f"The task '{task_title}' names its spec outside .ai-factory/\n{relpath}"
            )
        if ".." in relpath.split("/"):
            raise HaltError(
                f"The task '{task_title}' names its spec with a '..' segment\n{relpath}"
            )
        spec_path = self.project_dir / relpath
        try:
            data = spec_path.read_bytes()
        except FileNotFoundError:
            raise HaltError(
                f"The task '{task_title}' has no spec file at its 'Spec:' tag\n{spec_path}"
            ) from None
        except OSError as e:
            raise HaltError(
                f"The task '{task_title}' has a spec that cannot be read\n{spec_path}\n{e}"
            ) from None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HaltError(
                f"The task '{task_title}' has a spec that is not UTF-8\n{spec_path}"
            ) from None
        if not text.strip():
            raise HaltError(
                f"The task '{task_title}' has an empty spec\n{spec_path}"
            )
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_bytes(f"# Plan: {task_title}\n\n".encode("utf-8") + data)


class CodeReviewer:
    """Reviews code changes in a session of its own, never the planner's."""

    def __init__(self, project_dir: Path, backend: AgentBackendProtocol):
        self.project_dir = project_dir
        self.backend = backend
        self.system_prompt = _compose_instructions(_load_prompt("reviewer"), _load_prompt("escalation"))
        self.session_id: str | None = None

    def review(self, plan_path: Path, review_path: Path, prev_review_path: Path | None = None, implementer_answer_path: Path | None = None, *, iteration_state: IterationState) -> bool:
        """Review code changes in the reviewer's own session.

        The message carries the plan, the implementer's answer and, on a
        re-review, the previous review, each under its heading, and names the
        base and the iteration's commit on the run's ref, so the reviewer reads
        the changes as the difference between them wherever it lives. The run
        writes the reviewer's answer to review_path; an empty answer halts with
        EmptyAnswerError and leaves ``step`` at ``implemented:<N>``, so a
        resumed run repeats the review.
        """
        sections = [
            "## Plan",
            "",
            _read_artifact(plan_path),
        ]
        if implementer_answer_path is not None:
            sections += ["", "## The implementer's answer", "", _read_artifact(implementer_answer_path)]
        if prev_review_path:
            sections += ["", "## The previous review", "", _read_artifact(prev_review_path)]
        body = "\n".join(sections)

        naming_lines = (
            f"Run `git diff {iteration_state.base} {iteration_state.commit}` to see ALL changes: "
            f"the base is `{iteration_state.base}`, the iteration's commit is `{iteration_state.commit}` "
            f"on the ref `{iteration_state.ref}`.\n"
            "The plan for context is under `## Plan`.\n"
        )
        if implementer_answer_path is not None:
            naming_lines += "The implementer's answer is under `## The implementer's answer`.\n"

        if prev_review_path:
            prompt = (
                "This is a RE-REVIEW after fixes were applied to address the previous review under `## The previous review`.\n"
                "Do NOT trust your session memory about file contents — the code has changed since your last pass.\n"
                "For each finding in the previous review, open the cited file as it is now and read it in full, "
                "quote the CURRENT content relevant to that finding, and give a verdict: Fixed or Not fixed, "
                "with the quote as evidence.\n\n"
                "Then run the normal full review for new issues:\n"
                "Review the CODE CHANGES for bugs, security issues, and correctness problems.\n"
                f"{naming_lines}"
                "Read each changed/new file IN FULL — understand the surrounding code, not just the diff.\n"
                "Think about what will break at runtime: missing migrations, type mismatches, race conditions, etc.\n"
                "Return your full review (per-finding verdicts plus any new issues) as your answer.\n"
                f"If you have no findings at all, end your answer with {REVIEW_VERIFY.pass_signal} on its own line.\n"
                f"\n{body}"
            )
        else:
            prompt = (
                "Review the CODE CHANGES for bugs, security issues, and correctness problems.\n"
                f"{naming_lines}"
                "Read each changed/new file IN FULL — understand the surrounding code, not just the diff.\n"
                "Think about what will break at runtime: missing migrations, type mismatches, race conditions, etc.\n"
                "Return your full review as your answer.\n"
                f"If you have no findings at all, end your answer with {REVIEW_VERIFY.pass_signal} on its own line.\n"
                f"\n{body}"
            )

        answer, self.session_id = self.backend.run(
            role=AgentRole.CODE_REVIEWER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=self.session_id,
            exchange=_exchange_of(self.project_dir, review_path),
        )
        _write_session(plan_path, "code-reviewer", self.session_id)
        _write_artifact(review_path, answer)
        # Look for the review kind's pass signal on its own line in the answer
        if _has_escalation(answer):
            excerpt = _escalation_excerpt(answer)
            _write_session(plan_path, "escalation", f"reviewer: {excerpt} ({review_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{review_path}: {excerpt}")

        return _has_signal(answer, REVIEW_VERIFY.pass_signal)

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None, implementer_answer_path: Path | None, iteration_state: IterationState) -> bool:
        return self.review(plan_path, out_path, prev_review_path=prev_out_path, implementer_answer_path=implementer_answer_path, iteration_state=iteration_state)


class PlanReviewer:
    """Reviews a plan before implementation. Fresh session — no planner bias."""

    def __init__(self, project_dir: Path, backend: AgentBackendProtocol):
        self.project_dir = project_dir
        self.backend = backend
        self.system_prompt = _compose_instructions(_load_prompt("reviewer"), _load_prompt("escalation"))

    def review_plan(self, plan_path: Path, review_path: Path, attempt: int) -> bool:
        """Review a plan. The run writes the reviewer's answer to review_path.

        The plan's text travels in the message under a ``## Plan`` heading; the
        reviewer's session id is written to the plan's sidecar under
        ``plan-reviewer-<attempt>`` as soon as the agent returns, before the
        answer is written, so a reviewer that answered nothing or escalated
        still leaves its id.

        Raises EmptyAnswerError when the agent's answer is empty — a halt, not
        a failed review; no review file is written.
        """
        prompt = (
            "Review the PLAN below.\n"
            "Read the codebase it targets.\n"
            "Check for: missing steps, wrong assumptions about the codebase, "
            "architectural mistakes, missing migrations, security issues, "
            "incorrect file paths or API usage.\n"
            "Return your full review as your answer.\n"
            f"If the plan is solid, end your answer with {PLAN_REVIEW_PASS} on its own line.\n"
            "\n"
            "## Plan\n"
            "\n"
            f"{_read_artifact(plan_path)}"
        )

        answer, sid = self.backend.run(
            role=AgentRole.PLAN_REVIEWER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=None,
            exchange=_exchange_of(self.project_dir, review_path),
        )
        _write_session(plan_path, f"plan-reviewer-{attempt}", sid)
        _write_artifact(review_path, answer)
        if _has_escalation(answer):
            excerpt = _escalation_excerpt(answer)
            _write_session(plan_path, "escalation", f"plan-reviewer: {excerpt} ({review_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{review_path}: {excerpt}")
        return _has_signal(answer, PLAN_REVIEW_PASS)


class Implementer:
    """Implements the plan, then applies the review feedback for the current iteration. Same session.

    How the turn's work reaches the run's tree is the supplied result
    implementation's: its preparation runs before the backend call of a task's
    first call, its channel is appended to the assignment, and its lay runs after
    the answer is written and not escalated.
    """

    def __init__(self, project_dir: Path, backend: AgentBackendProtocol, result: ImplementerResultProtocol):
        self.project_dir = project_dir
        self.backend = backend
        self.result = result
        self.system_prompt = _compose_instructions(_load_prompt("implementer"), _load_prompt("escalation"))
        self.session_id: str | None = None

    def implement(self, plan_path: Path, answer_path: Path, feedback_path: Path | None = None, roadmap_path: Path | None = None, line_number: int | None = None) -> None:
        """Implement the plan. The run writes the implementer's answer to answer_path.

        The first message carries the roadmap line and the plan's text under a
        ``## Plan`` heading; a fix message, sent when the role holds a session and
        a feedback path, carries the review's text under ``## Review``. Both end
        with the grammar of the answer: what was done, and one ``DEVIATION`` line
        per point where the file disagreed with the plan. No message names a path
        of the run's tree other than the roadmap line.

        The implementer's session id is written to the plan's sidecar under
        ``implementer`` as soon as the agent returns, before the answer is
        written. An empty answer halts with EmptyAnswerError and leaves ``step``
        at its value before the turn, so a resumed run calls the implementer
        again.
        """
        answer_grammar = (
            "Your answer states what was done, and carries one "
            "`DEVIATION: <plan said / file showed / done>` line for each point "
            "where the file disagreed with the plan."
        )
        first = self.session_id is None
        if first:
            self.result.prepare(plan_path)
        if self.session_id and feedback_path is not None:
            # Continuing with feedback — apply the fixes
            prompt = (
                "Apply the fixes of the review below.\n"
                "\n"
                "## Review\n"
                "\n"
                f"{_read_artifact(feedback_path)}\n"
                "\n"
                f"{answer_grammar}"
            )
        else:
            roadmap_line = ""
            if roadmap_path is not None and line_number is not None:
                roadmap_line = f"Roadmap: {roadmap_path} (line {line_number + 1})\n"

            prompt = (
                f"{roadmap_line}"
                "Implement the plan below.\n"
                "\n"
                "## Plan\n"
                "\n"
                f"{_read_artifact(plan_path)}\n"
                "\n"
                f"{answer_grammar}"
            )

        channel = self.result.channel_text(plan_path, first=first)
        if channel:
            prompt = f"{prompt}\n\n{channel}"

        answer, self.session_id = self.backend.run(
            role=AgentRole.IMPLEMENTER,
            prompt=prompt,
            cwd=str(self.project_dir),
            instructions=self.system_prompt,
            session_id=self.session_id,
        )
        _write_session(plan_path, "implementer", self.session_id)
        _write_artifact(answer_path, answer)

        if _has_escalation(answer):
            excerpt = _escalation_excerpt(answer)
            _write_session(plan_path, "escalation", f"implementer: {excerpt} ({answer_path})")
            _write_session(plan_path, "step", "escalated")
            raise EscalationError(f"{answer_path}: {excerpt}")

        self.result.lay(plan_path)


class TestRunner:
    """Runs the test command from the plan file and captures output. No LLM."""

    __test__ = False  # not a pytest test class; stops pytest collecting it by name

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def verify(self, plan_path: Path, out_path: Path, prev_out_path: Path | None, implementer_answer_path: Path | None, iteration_state: IterationState) -> bool:
        """Extract test command from plan, run it, write output. Returns True if exit code 0.

        Reads nothing from prev_out_path, implementer_answer_path or the
        iteration's state — the test run reads only the plan and the tree."""
        cmd = self._extract_test_command(plan_path)
        if not cmd:
            out_path.write_text("ERROR: No '## Test Command' section found in plan.\n", encoding="utf-8")
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
        out_path.write_text(output, encoding="utf-8")
        status = "PASSED" if passed else "FAILED"
        print(f"--- TestRunner: {status} (exit {result.returncode}, {elapsed:.1f}s) ---")
        return passed

    @staticmethod
    def _extract_test_command(plan_path: Path) -> str | None:
        """Read `## Test Command` section from the plan and return the command string."""
        lines = _read_artifact(plan_path).splitlines()
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

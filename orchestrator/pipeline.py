"""The flow: the per-task pipeline, the mode record and its instances, and the roadmap loop."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

from .agents import REVIEW_VERIFY, TEST_RUN_VERIFY, CodeReviewer, Implementer, Planner, PlanReviewer
from .artifact_layout import (
    PLAN_REVIEWS_DIRNAME,
    PLAN_SUFFIX,
    PLANS_DIRNAME,
    REVIEWS_DIRNAME,
    SIDECAR_SUFFIX,
    artifact_dir,
    implementer_answer_name,
    plan_name,
    plan_review_name,
    sidecar_path,
    task_stem,
)
from .config import _ALL_ROLES, OrchestratorConfig, role_runtimes
from .errors import EscalationError, HaltError, PipelineStopError
from .exchange import remove_task
from .git_runner import run_git_sync
from .implementer_result import ref_name_of
from .iteration_ref import make_iteration_commit, push_iteration_ref, remove_iteration_refs
from .notify import Outcome, report
from .ports import AgentBackendProtocol, AgentRole, ImplementerResultProtocol, IterationState, PlannerProtocol, Repository, VerifyKind
from .protocol import _read_artifact, _read_sessions, _write_session, _write_sessions
from .resume import _attempt_budget, _check_attempt, _detect_step, _interpret_step, _validate_sidecar_step, plan_review_budget
from .roadmap import mark_done, parse_roadmap
from .runtime import _run_summary
from . import state


def _make_llm_planner(project_dir: Path, backend: AgentBackendProtocol, prompt_name: str) -> PlannerProtocol:
    """The plan stage with a planner: plan in a session of its own."""
    return Planner(project_dir, backend, planner_prompt_name=prompt_name)


class Mode(NamedTuple):
    """Static per-mode divergences between the implement and test pipelines."""
    roadmap_relpath: str  # path relative to `.ai-factory/`
    header_label: str
    planner_prompt_name: str
    verify: VerifyKind
    artifact_subdir: str | None = None  # per-roadmap artifact dir segment; None = flat (default pair)
    make_planner: Callable[[Path, AgentBackendProtocol, str], PlannerProtocol] = _make_llm_planner
    roles: tuple[AgentRole, ...] = _ALL_ROLES


IMPLEMENT_MODE = Mode(
    roadmap_relpath="ROADMAP.md",
    header_label="TASK",
    planner_prompt_name="planner",
    verify=REVIEW_VERIFY,
)

TEST_MODE = Mode(
    roadmap_relpath="ROADMAP_TESTS.md",
    header_label="TEST TASK",
    planner_prompt_name="test-planner",
    verify=TEST_RUN_VERIFY,
)


def _next_number(directory: Path) -> int:
    """Return the next sequential number based on existing files in directory."""
    existing = [f for f in directory.glob("*") if f.suffix in (PLAN_SUFFIX, SIDECAR_SUFFIX) and not f.is_dir()]
    numbers = [
        int(parts[0])
        for f in existing
        if (parts := f.stem.split("-", 1))[0].isascii() and parts[0].isdigit()
    ]
    if numbers:
        return max(numbers) + 1
    return sum(f.suffix == PLAN_SUFFIX for f in existing) + 1


def _artifact_dir(project_dir: Path, mode: Mode, dirname: str) -> Path:
    """The artifact directory `dirname`, under the mode's per-roadmap subdirectory when it has one; creates nothing."""
    return artifact_dir(project_dir, dirname, mode.artifact_subdir)


def _raise_git_failure(result: subprocess.CompletedProcess) -> None:
    """Fail a scrubbed git call the way ``check=True`` failed: the captured
    standard error, when the call holds one, goes to the console first, as
    git wrote it — a call left uncaptured has already put it there directly —
    then the completed process raises its ``CalledProcessError`` — the
    command, the status and the captured output aboard."""
    if result.stderr:
        sys.stderr.write(result.stderr)
    result.check_returncode()


def _git_commit(tree: tuple[Repository, ...], task_title: str) -> None:
    """Stage, commit and push after a completed task, once for each
    repository of the run's tree in its order — the project's first.  A
    repository without a staged change of its own prints the skip line and
    the loop goes on to the next repository, never skipping it; the other
    repository's check and commit are scoped to the folder `.ai-factory`
    resolves to, so another project's staged change stays staged and out of
    the task's commit."""
    for repository in tree:
        _raise_git_failure(run_git_sync(list(repository.stage), cwd=repository.directory))
        # Check if there's anything to commit
        result = run_git_sync(["diff", "--cached", "--quiet", *repository.scope], cwd=repository.directory)
        if result.returncode == 0:
            print(">>> Nothing to commit, skipping.")
            continue
        # The commit runs the target project's own hooks, so its duration is
        # theirs: it takes no bound.  It reads none of git's output either,
        # so the two streams stay uncaptured — a hook's background job then
        # holds no pipe the commit waits for.
        _raise_git_failure(run_git_sync(
            ["commit", "-m", task_title, *repository.scope], cwd=repository.directory, timeout=None,
            capture=False,
        ))
        print(f">>> COMMITTED: {task_title}")

        try:
            push_result = run_git_sync(["push", "-u", "origin", "HEAD"], cwd=repository.directory)
        except subprocess.TimeoutExpired:
            print(">>> WARNING: git push failed, continuing anyway.")
            continue
        if push_result.returncode != 0:
            print(">>> WARNING: git push failed, continuing anyway.")


def _task_bases(tree: tuple[Repository, ...]) -> tuple[str, ...]:
    """The commit each repository of the run's tree is at now, in the tree's
    order — the task's base where the record holds none yet, as a start's
    is taken. A repository whose `HEAD` cannot be read halts naming the
    repository and git's own text."""
    bases = []
    for repository in tree:
        head = run_git_sync(["rev-parse", "HEAD"], cwd=repository.directory)
        if head.returncode != 0:
            raise HaltError(
                "The task's base could not be recorded\n"
                f"{repository.label}\n{head.stderr.strip()[:500]}"
            )
        bases.append(head.stdout.strip())
    return tuple(bases)


def _record_planning(plan_path: Path, runtimes: dict[str, dict[str, str]], attempt: int, session_id: str | None, bases: tuple[str, ...]) -> None:
    """Persist pending intent and the effective continuation before invoking planning."""
    updates = {"runtime": runtimes, "step": f"planning:{attempt}", "bases": list(bases)}
    if session_id is not None:
        updates["planner"] = session_id
    _write_sessions(plan_path, updates)


def process_task(project_dir: Path, task, task_index: int, config: OrchestratorConfig, mode: Mode = IMPLEMENT_MODE, phase_session_id: str | None = None, *, backend: AgentBackendProtocol, implementer_result: ImplementerResultProtocol, tree: tuple[Repository, ...]) -> str | None:
    """Plan → implement → verify loop for a single task (verify = review, or a real test run in test mode)."""
    max_iterations = config.max_iterations
    runtimes = role_runtimes(config.runtime, config.roles, mode.roles)
    plans_dir = _artifact_dir(project_dir, mode, PLANS_DIRNAME)
    output_dir = _artifact_dir(project_dir, mode, mode.verify.output_dirname)
    plan_reviews_dir = _artifact_dir(project_dir, mode, PLAN_REVIEWS_DIRNAME)
    reviews_dir = _artifact_dir(project_dir, mode, REVIEWS_DIRNAME)
    plans_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_reviews_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(parents=True, exist_ok=True)

    roadmap_path = project_dir / ".ai-factory" / mode.roadmap_relpath
    seq = f"{task_index:02d}"
    plan_path = plans_dir / plan_name(seq, task.slug)
    print(f"\n{'='*60}")
    print(f"{mode.header_label}: {task.title}")
    print(f"{'='*60}")

    step, counter, plan_path = _detect_step(
        seq, task.slug, plan_path, plan_reviews_dir, output_dir, mode.verify,
        max_iterations=max_iterations, runtimes=runtimes,
        plan_review_iterations=config.plan_review_iterations, planning=config.planning,
    )
    seq = plan_path.stem.split("-", 1)[0]
    stem = task_stem(seq, task.slug)

    sessions = _read_sessions(plan_path)
    if not sessions:
        # A task started afresh never meets an earlier attempt's message,
        # marker or answer under the same plan stem; a resume keeps its files.
        remove_task(project_dir, plan_path)
    elapsed_offset = int(sessions.get("elapsed", "0"))
    task_start = time.monotonic() - elapsed_offset

    if step != "plan":
        print(f">>> Resuming from step '{step}' (counter={counter})")

    if step == "escalated":
        raise EscalationError(sessions.get("escalation", "(no escalation detail recorded)"))

    _check_attempt(step, counter, *_attempt_budget(step, max_iterations, config.plan_review_iterations, config.planning))

    # The task's base in each repository: what the record holds, or the
    # commit each repository is at now where it holds none — a task begun
    # by an older run taking its base as a start does — written with the
    # record's first write below and through every iteration kept as it is.
    # One commit per repository of the tree, in its order: a record whose
    # count no longer matches the tree — a task whose `.ai-factory` link
    # changed beneath it among them — halts here, before any work is spent.
    recorded = sessions.get("bases")
    bases = tuple(recorded) if recorded is not None else _task_bases(tree)
    if len(bases) != len(tree):
        raise HaltError(
            "The task's recorded bases do not match the run's tree\n"
            f"{sidecar_path(plan_path)}\n{len(bases)} recorded, {len(tree)} repositories"
        )

    raw_boundary = sessions.get("step", "")
    valid_boundary = _interpret_step(raw_boundary, mode.verify) is not None and _validate_sidecar_step(
        raw_boundary, seq, task.slug, plan_reviews_dir, output_dir,
        mode.verify.fail_tag, mode.verify.output_suffix,
    ) == raw_boundary
    boundary = raw_boundary if valid_boundary else {
        "plan": f"planning:{counter}",
        "plan_review": f"planned:{counter}",
        "implement": "plan_reviewed",
    }[step]
    # Inferred planning is adopted by the pending write immediately before its call.
    if ("runtime" not in sessions or boundary != raw_boundary) and not (step == "plan" and not valid_boundary):
        _write_sessions(plan_path, {"runtime": runtimes, "step": boundary, "bases": list(bases)})
    elif "runtime" in sessions and "bases" not in sessions:
        # A record this run found holding no bases: it takes them on its own
        # write, merged into the runtime it already holds. A record without
        # runtime never takes them alone — the runtime write above carries
        # them, so a resume never meets a bases-only record.
        _write_sessions(plan_path, {"bases": list(bases)})

    # Create agents
    planner = mode.make_planner(project_dir, backend, mode.planner_prompt_name)
    code_reviewer = CodeReviewer(project_dir, backend)
    implementer = Implementer(
        project_dir, backend,
        result=implementer_result,
    )
    verifier = mode.verify.make_verifier(code_reviewer, project_dir)

    planner.session_id = sessions["planner"] if "planner" in sessions else phase_session_id
    implementer.session_id = sessions.get("implementer")
    code_reviewer.session_id = sessions.get("code-reviewer")

    # Step 1: Plan
    if step == "plan":
        print("\n>>> PLANNING...")
        _record_planning(plan_path, runtimes, counter, planner.session_id, bases)
        if counter > 1:
            prev_plan_review = plan_reviews_dir / plan_review_name(seq, task.slug, counter - 1)
            planner.plan(task.title, task.description, plan_path, roadmap_path=roadmap_path, line_number=task.line_number, plan_review_path=prev_plan_review)
        else:
            planner.plan(task.title, task.description, plan_path, roadmap_path=roadmap_path, line_number=task.line_number)

        step = "plan_review"
        _write_session(plan_path, "step", f"planned:{counter}")
        _write_session(plan_path, "elapsed", str(int(time.monotonic() - task_start)))

    # Step 1.5: Iterative plan review
    if step in ("plan", "plan_review"):
        budget = plan_review_budget(max_iterations, config.plan_review_iterations)
        for attempt in range(counter, budget + 1):
            print(f"\n>>> REVIEWING PLAN (attempt {attempt})...")
            plan_reviewer = PlanReviewer(project_dir, backend)
            plan_review_path = plan_reviews_dir / plan_review_name(seq, task.slug, attempt)
            plan_passed = plan_reviewer.review_plan(plan_path, plan_review_path, attempt)
            _write_session(plan_path, "elapsed", str(int(time.monotonic() - task_start)))

            if plan_passed:
                print(f">>> Plan review passed — see {plan_review_path}")
                _write_session(plan_path, "step", "plan_reviewed")
                break

            if attempt == budget:
                raise PipelineStopError(
                    f"Plan failed\n\n"
                    f"Last review: {plan_review_path}\n\n{_read_artifact(plan_review_path)}"
                )

            _check_attempt("plan", attempt + 1, *_attempt_budget("plan", max_iterations, config.plan_review_iterations, config.planning))
            print(">>> Plan has issues — revising plan...")
            _write_session(plan_path, "step", f"plan_review_failed:{attempt}")
            _record_planning(plan_path, runtimes, attempt + 1, planner.session_id, bases)
            planner.plan(
                task.title, task.description, plan_path,
                plan_review_path=plan_review_path,
                roadmap_path=roadmap_path, line_number=task.line_number,
            )
            _write_session(plan_path, "step", f"planned:{attempt + 1}")

    # Step 2-3: Implement → Verify loop
    impl_start = counter if step in ("implement", mode.verify.step) else 1
    for iteration in range(impl_start, max_iterations + 1):
        if step == mode.verify.step and iteration == counter:
            # Resuming mid-verify: implementation already done, go straight to verify
            pass
        else:
            print(f"\n>>> IMPLEMENTING (iteration {iteration})...")
            feedback_path = output_dir / (stem + mode.verify.output_suffix.format(n=iteration - 1)) if iteration > 1 else None
            answer_path = reviews_dir / implementer_answer_name(seq, task.slug, iteration)
            implementer.implement(plan_path, answer_path, feedback_path=feedback_path, roadmap_path=roadmap_path, line_number=task.line_number)
            _write_session(plan_path, "step", f"implemented:{iteration}")
            _write_session(plan_path, "elapsed", str(int(time.monotonic() - task_start)))

        print(f"\n>>> {mode.verify.running_header} (iteration {iteration})...")
        iteration_states: list[IterationState] = []
        for index, repository in enumerate(tree):
            _raise_git_failure(run_git_sync(list(repository.stage), cwd=repository.directory))
            iteration_state = make_iteration_commit(
                repository, ref_name_of(project_dir, plan_path), iteration, bases[index],
            )
            push_iteration_ref(iteration_state)
            iteration_states.append(iteration_state)
        out_path = output_dir / (stem + mode.verify.output_suffix.format(n=iteration))
        prev_out_path = None
        if iteration > 1:
            prev = output_dir / (stem + mode.verify.output_suffix.format(n=iteration - 1))
            if prev.exists():
                prev_out_path = prev
        implementer_answer = reviews_dir / implementer_answer_name(seq, task.slug, iteration)
        passed = verifier.verify(plan_path, out_path, prev_out_path,
                                 implementer_answer if implementer_answer.exists() else None,
                                 tuple(iteration_states))
        _write_session(plan_path, "elapsed", str(int(time.monotonic() - task_start)))

        if passed:
            print(f">>> {mode.verify.pass_line.format(path=out_path)}")
            break
        else:
            print(f">>> {mode.verify.fail_line.format(path=out_path)}")
            _write_session(plan_path, "step", f"{mode.verify.fail_tag}{iteration}")
            if iteration == max_iterations:
                raise PipelineStopError(
                    mode.verify.max_iterations_message.format(n=max_iterations, path=out_path, content=_read_artifact(out_path))
                )

    # Step 4: Mark done + commit
    elapsed = int(time.monotonic() - task_start)
    mark_done(roadmap_path, task, elapsed)
    state.tasks_done += 1
    _write_session(plan_path, "step", "done")
    _git_commit(tree, task.title)
    remove_iteration_refs(project_dir, tree, plan_path)
    remove_task(project_dir, plan_path)  # a sweep of the files of a call that never got its answer
    report(config, Outcome.TASK_DONE, project_dir.name, task.title, _run_summary())

    mins, secs = divmod(elapsed, 60)
    print(f">>> Task done [{mins}m {secs}s]")
    return planner.session_id


def _run_dynamic_loop(project_dir: Path, roadmap_path: Path, config: OrchestratorConfig, process_fn, mode: Mode, *, usage_check: Callable[[], None]) -> None:
    """Dynamically re-scan the roadmap before each task, always running the first unchecked one."""
    plans_dir = _artifact_dir(project_dir, mode, PLANS_DIRNAME)
    plans_dir.mkdir(parents=True, exist_ok=True)
    phase_sessions_enabled = config.enable_phase_sessions

    # Startup summary (printed once)
    result = parse_roadmap(roadmap_path)
    pending = [m for m in result.tasks if not m.done]
    if not pending:
        if not state.stop_requested:
            state.queue_exhausted = True
        print("All tasks are done!")
        return
    if result.breakpoint_hit:
        total = len(result.tasks) + result.tasks_after_breakpoint
        print(f"Found {len(pending)} pending tasks out of {total} total (stopped at breakpoint — {result.tasks_after_breakpoint} tasks after marker not queued).")
    else:
        print(f"Found {len(pending)} pending tasks out of {len(result.tasks)} total.")

    current_section: str | None = None
    phase_session_id: str | None = None
    last_signature: tuple[str, str] | None = None

    while not state.stop_requested:
        result = parse_roadmap(roadmap_path)
        pending = [m for m in result.tasks if not m.done]
        if not pending:
            if not state.stop_requested:
                state.queue_exhausted = True
            report(config, Outcome.RUN_DONE, project_dir.name, None, _run_summary())
            break

        task = pending[0]
        signature = (task.title, task.description)
        if signature == last_signature:
            raise PipelineStopError(
                f"Task '{task.title}' checkbox is still unchecked after processing. "
                f"Refusing to re-run the same task forever — check mark_done."
            )
        last_signature = signature

        i = _next_number(plans_dir)
        usage_check()

        if task.section != current_section:
            current_section = task.section
            phase_session_id = None
        elif not phase_sessions_enabled:
            phase_session_id = None

        phase_session_id = process_fn(task, i, phase_session_id)

    if state.stop_requested:
        print("\n>>> Stop requested — halting.")
        report(config, Outcome.MANUAL_STOP, project_dir.name, None, _run_summary())

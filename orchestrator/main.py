"""Composition root and CLI: assemble the mode for a run and hand it to the flow."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import sys
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Callable, NoReturn

from .agents import SpecLayer
from .artifact_layout import PLAN_REVIEWS_DIRNAME, PLANS_DIRNAME, REVIEWS_DIRNAME, TEST_RUNS_DIRNAME
from .claude_cli import ClaudeAgentBackend
from .codex_cli import CodexAgentBackend
from .config import OrchestratorConfig, load_config, role_runtimes
from .errors import EscalationError, HaltError, HeadNotReachedError, PipelineStopError, RunDataRefusal
from .exchange import answer_command
from .head_backend import HeadAgentBackend
from .hermes_api import HermesAgentBackend
from .git_runner import run_git_sync
from .implementer_result import HermesResult, WorkingTreeResult
from .locator import HeadIdentity, LocatorFault, read_address
from .role_backend import RoleRoutedBackend, backend_key
from .layout import _artifact_subdir, _external_evidence_path, _resolve_roadmap_relpath, _tests_sibling
from .notify import Outcome, report
from .pipeline import IMPLEMENT_MODE, TEST_MODE, Mode, _artifact_dir, _run_dynamic_loop, process_task
from .ports import AgentBackendProtocol, AgentRole, ImplementerResultProtocol
from .resume import plan_review_budget
from .run_data import dispose, refuse, rollback, show, sweep
from .runtime import _handle_sigint, _run_summary, _with_caffeinate
from .usage import _check_codex_usage_limits, _check_usage_limits
from .user_slug import USER_SLUG_MISSING, user_slug
from . import state


_IMPLEMENTER_RESULT_DIRS = (PLANS_DIRNAME, PLAN_REVIEWS_DIRNAME, REVIEWS_DIRNAME, TEST_RUNS_DIRNAME)


def _factory_tracked(project_dir: Path) -> bool:
    """Whether the commit the run is at holds at least one path beneath the
    project's .ai-factory, which is what his clone, built from commits, holds."""
    try:
        listed = run_git_sync(
            ["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ".ai-factory/"],
            cwd=project_dir,
        )
    except OSError:
        return False
    return listed.returncode == 0 and any(listed.stdout.split("\0"))


def _implementer_result(config: OrchestratorConfig, project_dir: Path, mode: Mode) -> ImplementerResultProtocol:
    """Choose how the implementer's result reaches the run's tree, by its runtime.

    An implementer on Hermes works in his own clone: his result is a pushed
    branch, laid as the tree outside the run's own artifact paths for the
    roadmap in play. His clone holds only what the project's repository
    tracks, so a project whose .ai-factory git does not track — gitignored,
    a nested repository, a symlink out — cannot have him as its implementer.
    """
    entry = role_runtimes(config.runtime, config.roles)["implementer"]
    if entry["runtime"] != "hermes":
        return WorkingTreeResult()
    if not _factory_tracked(project_dir):
        raise SystemExit(
            "Invalid 'roles' role 'implementer': .ai-factory is not tracked in the project's "
            "repository, so his branch holds none of it"
        )
    preserved = tuple(_artifact_dir(project_dir, mode, name) for name in _IMPLEMENTER_RESULT_DIRS)
    return HermesResult(project_dir, preserved)


def _print_run_choices(config: OrchestratorConfig) -> None:
    """Print the stages the run runs after the agent lines: planning, and the plan-review budget when planning is on."""
    print(f"Planning: {'on' if config.planning else 'off'}")
    if config.planning:
        print(f"Plan review iterations: {plan_review_budget(config.max_iterations, config.plan_review_iterations)}")


def _make_runtime(config: OrchestratorConfig, project_dir: Path, roles: tuple[AgentRole, ...]) -> tuple[AgentBackendProtocol, Callable[[], None]]:
    """Construct one backend per runtime a role of the run takes and bind the pre-task usage policy."""
    print(f"Agent runtime: {config.runtime}")
    runtimes = role_runtimes(config.runtime, config.roles, roles)
    taken = {entry["runtime"] for entry in runtimes.values()}
    heads: dict[str, HeadIdentity] = {}
    for entry in runtimes.values():
        if entry["runtime"] == "head":
            head = entry["head"]
            identity = HeadIdentity(Path(head["repository"]), head["owner"], head["folder"])
            heads[backend_key(entry)] = identity
    if heads:
        command = answer_command()
        if not (command.is_file() and os.access(command, os.X_OK)):
            raise HeadNotReachedError(
                "A head could not be reached\n"
                f"The answer command {command} does not exist or is not executable"
            )
    for key, identity in sorted(heads.items()):
        try:
            read_address(identity)  # a head's address is read fresh, before any backend is built
        except LocatorFault as fault:
            raise HeadNotReachedError(
                "A head could not be reached\n"
                f"{identity.repository} {identity.owner} {identity.folder}\n{fault}"
            ) from None
    backends: dict[str, AgentBackendProtocol] = {}
    built: set[str] = set()
    for entry in runtimes.values():
        key = backend_key(entry)
        if key in built:
            continue
        built.add(key)
        runtime = entry["runtime"]
        if runtime == "claude":
            backends[key] = ClaudeAgentBackend(config.claude_agents)
        elif runtime == "codex":
            backends[key] = CodexAgentBackend(config.codex_agents)
        elif runtime == "hermes":
            settings = config.hermes_instances[entry["instance"]]
            backends[key] = HermesAgentBackend(settings["server"], settings["port"], settings["key_file"])
        else:
            backends[key] = HeadAgentBackend(heads[key])
    backend = RoleRoutedBackend(backends, runtimes)
    for role in roles:
        entry = runtimes[role.value]
        if entry["runtime"] == "hermes":
            print(f"  {role.value}: hermes {entry['instance']}")
        elif entry["runtime"] == "head":
            head = entry["head"]
            print(f"  {role.value}: head {head['repository']} {head['owner']} {head['folder']}")
        else:
            settings = backend.role_settings[role]
            print(f"  {role.value}: {entry['runtime']} {settings.model} ({settings.effort})")
    checks: list[Callable[[], None]] = []
    if "claude" in taken:
        checks.append(partial(_check_usage_limits, config))
    if "codex" in taken:
        checks.append(partial(_check_codex_usage_limits, config, project_dir))

    def usage_check() -> None:
        for check in checks:
            check()

    return backend, usage_check


def _test_loop(project_dir: Path, config: OrchestratorConfig) -> None:
    """Write tests for all pending tasks from the roadmap's test sibling."""
    main_relpath = _resolve_roadmap_relpath(config, project_dir)
    relpath = _tests_sibling(main_relpath)
    roadmap_path = project_dir / ".ai-factory" / relpath
    if not roadmap_path.exists():
        print(f"ERROR: No roadmap found at {roadmap_path}")
        sys.exit(1)
    mode = TEST_MODE._replace(roadmap_relpath=relpath, artifact_subdir=_artifact_subdir(relpath))
    if not config.planning:
        # Test mode plans and reviews plans whatever the setting: the run hands
        # on the stage it runs.
        config = dataclasses.replace(config, planning=True)
    backend, usage_check = _make_runtime(config, project_dir, mode.roles)
    implementer_result = _implementer_result(config, project_dir, mode)
    _print_run_choices(config)
    _run_dynamic_loop(
        project_dir, roadmap_path, config,
        lambda m, i, sid: process_task(project_dir, m, i, config, mode, phase_session_id=sid, backend=backend, implementer_result=implementer_result),
        mode=mode,
        usage_check=usage_check,
    )


def _implement_loop(project_dir: Path, config: OrchestratorConfig, planner_prompt_name: str = "planner", roadmap_relpath: str | None = None) -> None:
    """Plan + implement all pending tasks. No review."""
    relpath = roadmap_relpath or _resolve_roadmap_relpath(config, project_dir)
    roadmap_path = project_dir / ".ai-factory" / relpath
    if not roadmap_path.exists():
        print(f"ERROR: No roadmap found at {roadmap_path}")
        sys.exit(1)
    mode = IMPLEMENT_MODE._replace(
        planner_prompt_name=planner_prompt_name, roadmap_relpath=relpath, artifact_subdir=_artifact_subdir(relpath),
    )
    if not config.planning:
        # The planning-off assembly leaves the planner and the plan reviewer
        # out of the run's roles and runs on a budget of 0.
        mode = mode._replace(
            make_planner=lambda project_dir, backend, prompt_name: SpecLayer(project_dir),
            roles=(AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER),
        )
        config = dataclasses.replace(config, plan_review_iterations=0)
    backend, usage_check = _make_runtime(config, project_dir, mode.roles)
    implementer_result = _implementer_result(config, project_dir, mode)
    _print_run_choices(config)
    _run_dynamic_loop(
        project_dir, roadmap_path, config,
        lambda m, i, sid: process_task(project_dir, m, i, config, mode, phase_session_id=sid, backend=backend, implementer_result=implementer_result),
        mode=mode,
        usage_check=usage_check,
    )


def run_implement(project_dir: Path, config: OrchestratorConfig) -> None:
    """Implement only — plan + implement tasks, no review pass."""
    state.queue_exhausted = False
    state.config = config
    state.project_dir = project_dir
    state.run_started = time.monotonic()
    state.tasks_done = 0
    signal.signal(signal.SIGINT, _handle_sigint)
    time_str = _with_caffeinate(_implement_loop, project_dir, config)
    print(f"\n{'='*60}")
    print(f"IMPLEMENT DONE — {time_str}")
    print(f"{'='*60}")


def run_test(project_dir: Path, config: OrchestratorConfig) -> None:
    """Test mode — plan + implement tests, gate on real test runner output."""
    state.queue_exhausted = False
    state.config = config
    state.project_dir = project_dir
    state.run_started = time.monotonic()
    state.tasks_done = 0
    signal.signal(signal.SIGINT, _handle_sigint)
    time_str = _with_caffeinate(_test_loop, project_dir, config)
    print(f"\n{'='*60}")
    print(f"TEST DONE — {time_str}")
    print(f"{'='*60}")


def _write_completion_proof(completion_path: Path, invocation_id: str, succeeded: bool) -> None:
    """Publish the correlated terminal result through a sibling temporary file."""
    temporary = completion_path.with_name(completion_path.name + ".tmp")
    temporary.write_text(json.dumps({
        "invocation_id": invocation_id,
        "outcome": "SUCCESS" if succeeded else "FAIL",
    }), encoding="utf-8")
    os.replace(temporary, completion_path)


def _run_settings_slug(project_dir: Path) -> str | None:
    """The slug that names the person's run settings file, derived where the
    project holds a run-settings directory.

    Where `.ai-factory/run-settings/` exists the person's file is looked for
    under the slug the git identity gives; a project that holds no such
    directory derives no slug and the run reads no person's file. Where the
    directory exists and no slug can be derived the run exits at its start
    with the shared no-slug text — a startup error, raised before the
    configuration loads, so no config exists to report a halt through.
    """
    if not (project_dir / ".ai-factory" / "run-settings").is_dir():
        return None
    slug = user_slug(project_dir)
    if slug is None:
        raise SystemExit(USER_SLUG_MISSING)
    return slug


def _refuse_usage(message: str) -> NoReturn:
    """Answer a malformed invocation of a command over a run's data the way
    the command refuses: one line on standard error, status 1 — never
    argparse's usage block."""
    raise RunDataRefusal(message)


def cli() -> None:
    state.queue_exhausted = False
    parser = argparse.ArgumentParser(description="AI orchestrator — plan, implement, review from roadmap")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    for cmd, help_text in [
        ("implement", "Plan and implement tasks"),
        ("test", "Write tests for tasks (uses test-planner prompt)"),
    ]:
        p = subparsers.add_parser(cmd, help=help_text)
        p.add_argument("project_dir", nargs="?", default=".", help="Path to the project directory")
        p.add_argument("--completion-file", help="External path for terminal completion proof")
        p.add_argument("--invocation-id", help="Canonical UUID correlated with completion proof")

    # A malformed invocation of a command over a run's data is refused like
    # any other refusal: one line, status 1 — the run's own commands keep
    # argparse's usage error.
    show_parser = subparsers.add_parser("show", help="Show what the orchestrator holds for a task or a phase, as JSON")
    show_parser.error = _refuse_usage
    show_parser.add_argument("--project-dir", help="Path to the project directory")
    show_parser.add_argument("--roadmap", help="The roadmap to read, as the run's roadmap_path takes it")
    show_parser.add_argument("task", nargs="?", help="The task's N.M, or a flat task's title as its line gives it")
    show_parser.add_argument("--phase", help="The phase's number")

    rollback_parser = subparsers.add_parser("rollback", help="Take a task back to a position show listed for it")
    rollback_parser.error = _refuse_usage
    rollback_parser.add_argument("--project-dir", help="Path to the project directory")
    rollback_parser.add_argument("--roadmap", help="The roadmap to read, as the run's roadmap_path takes it")
    rollback_parser.add_argument("task", help="The task's N.M, or a flat task's title as its line gives it")
    rollback_parser.add_argument("position", help="The position show lists for the task")

    sweep_parser = subparsers.add_parser("sweep", help="Remove the run's artifacts of a phase whose every task is done")
    sweep_parser.error = _refuse_usage
    sweep_parser.add_argument("--project-dir", help="Path to the project directory")
    sweep_parser.add_argument("--roadmap", help="The roadmap to read, as the run's roadmap_path takes it")
    sweep_parser.add_argument("--phase", help="The phase's number")

    dispose_parser = subparsers.add_parser("dispose", help="Record the dispositions of a closure's deposit read from standard input")
    dispose_parser.error = _refuse_usage
    dispose_parser.add_argument("--project-dir", help="Path to the project directory")

    try:
        args, extras = parser.parse_known_args()
    except RunDataRefusal as e:
        refuse(e)
    if extras:
        # What parse_args says here for the run's own commands, kept as it
        # was; a command over a run's data refuses the same leftovers as
        # one line with status 1.
        if args.command in ("show", "rollback", "sweep", "dispose"):
            refuse(RunDataRefusal(f"unrecognized arguments: {' '.join(extras)}"))
        parser.error(f"unrecognized arguments: {' '.join(extras)}")
    project_dir = Path(args.project_dir).resolve() if hasattr(args, "project_dir") and args.project_dir else Path(".").resolve()

    completion_file = getattr(args, "completion_file", None)
    invocation_id = getattr(args, "invocation_id", None)
    if (completion_file is None) != (invocation_id is None):
        parser.error("--completion-file and --invocation-id must be supplied together")
    completion_path = None
    if invocation_id is not None:
        try:
            if invocation_id != str(uuid.UUID(invocation_id)):
                raise ValueError("noncanonical UUID")
        except ValueError:
            parser.error("--invocation-id must be a canonical lowercase hyphenated UUID")
        try:
            completion_path = _external_evidence_path(project_dir, Path(completion_file))
        except ValueError as e:
            parser.error(str(e))

    if args.command in ("show", "rollback", "sweep", "dispose"):
        # The commands answer from the run's data alone: they load the
        # configuration as a run does and touch none of the run's state,
        # handlers or loops. A rollback changes the run's records, artifacts
        # and working tree — the head's exchange files among the things it
        # leaves where they stand — a sweep removes the run's artifacts of a
        # finished phase alone, and a dispose writes the dispositions
        # record alone, its deposit read from standard input.
        try:
            config = load_config(project_dir=project_dir, user_slug=_run_settings_slug(project_dir))
            if args.command == "show":
                show(config, project_dir, roadmap=args.roadmap, task=args.task, phase=args.phase)
            elif args.command == "rollback":
                rollback(config, project_dir, roadmap=args.roadmap, task=args.task, position=args.position)
            elif args.command == "sweep":
                sweep(config, project_dir, roadmap=args.roadmap, phase=args.phase)
            else:
                dispose(project_dir)
        except (RunDataRefusal, HaltError, SystemExit) as e:
            refuse(e)
        return

    completed_normally = False
    try:
        config = load_config(project_dir=project_dir, user_slug=_run_settings_slug(project_dir))

        try:
            if args.command == "test":
                run_test(project_dir, config)
            else:
                run_implement(project_dir, config)
            completed_normally = True
        except PipelineStopError as e:
            print(f"\n{'='*60}")
            print(f"STOPPED — {e}")
            print(f"{'='*60}")
            msg = str(e).splitlines()[0]
            report(config, Outcome.UNCONVERGED, project_dir.name, msg, _run_summary())
            sys.exit(0)
        except HaltError as e:
            print(f"\n{'='*60}")
            print(f"HALTED — {e}")
            print(f"{'='*60}")
            msg = str(e).splitlines()[0]
            report(config, Outcome.HALTED, project_dir.name, msg, _run_summary())
            sys.exit(0)
        except EscalationError as e:
            print(f"\n{'='*60}")
            print(f"ESCALATED — {e}")
            print(f"{'='*60}")
            report(config, Outcome.ESCALATED, project_dir.name, None, _run_summary())
            sys.exit(0)
        except Exception as e:
            report(
                config,
                Outcome.ERRORED,
                project_dir.name,
                f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}",
                _run_summary(),
            )
            raise
    finally:
        if completion_path is not None:
            try:
                _write_completion_proof(
                    completion_path, invocation_id,
                    completed_normally and state.queue_exhausted and not state.stop_requested,
                )
            except BaseException as e:
                # Optional evidence must preserve the run's own return or exception.
                try:
                    print(f"Could not publish completion proof: {e}", file=sys.stderr)
                except BaseException:
                    pass
                try:
                    completion_path.unlink(missing_ok=True)
                except BaseException:
                    pass
                try:
                    completion_path.with_name(completion_path.name + ".tmp").unlink(missing_ok=True)
                except BaseException:
                    pass


if __name__ == "__main__":
    cli()

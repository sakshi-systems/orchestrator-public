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
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Callable, NamedTuple, NoReturn, Sequence

from .agents import SpecLayer
from .artifact_layout import (
    DISPOSITIONS_DIRNAME,
    PLAN_REVIEWS_DIRNAME,
    PLANS_DIRNAME,
    REVIEWS_DIRNAME,
    TEST_RUNS_DIRNAME,
)
from .claude_cli import ClaudeAgentBackend
from .codex_cli import CodexAgentBackend
from .closure import DONE, CloseStep
from .config import OrchestratorConfig, load_config, role_runtimes
from .errors import EscalationError, HaltError, HeadNotReachedError, PipelineStopError, RunDataRefusal, RunStoppedError, ScenarioRefusal
from .exchange import answer_command
from .head_backend import HeadAgentBackend
from .hermes_api import HermesAgentBackend
from .git_runner import run_git_sync
from .implementer_result import HermesResult, WorkingTreeResult
from .locator import HeadIdentity, LocatorFault, read_address
from .role_backend import RoleRoutedBackend, backend_key
from .layout import _artifact_subdir, _external_evidence_path, _resolve_roadmap_relpath, _tests_sibling
from .notify import Outcome, report
from .pipeline import CLOSURE_OWED, IMPLEMENT_MODE, NO_TASK, TEST_MODE, Mode, _artifact_dir, _run_dynamic_loop, process_task
from .ports import AgentBackendProtocol, AgentRole, ImplementerResultProtocol, Repository, StepKindProtocol
from .repositories import find_run_tree
from .resume import plan_review_budget
from .run_data import dispose, owed_phase, refuse, rollback, show, sweep
from .scenario_file import Cycle, Scenario, Step, StepKind, StepOption, load_scenario, refusal
from .scenario_lookup import check_template, resolve_scenario, resolve_template
from .scenario_run import run_scenario
from .runtime import _handle_sigint, _run_summary, _with_caffeinate
from .usage import _check_codex_usage_limits, _check_usage_limits
from .user_slug import USER_SLUG_MISSING, user_slug
from . import state


_IMPLEMENTER_RESULT_DIRS = (
    PLANS_DIRNAME,
    PLAN_REVIEWS_DIRNAME,
    REVIEWS_DIRNAME,
    TEST_RUNS_DIRNAME,
    DISPOSITIONS_DIRNAME,
)

# The one help text for the `task` positional of `show` and `rollback`, so the
# two cannot differ: it names a task's two forms and a flat task's title, as
# docs/reference/run-data.md reads for `TASK`.
_TASK_HELP = "The task's N.M or a split child's N.M.k, or a flat task's title as its line gives it"


def _factory_tracked(project_dir: Path) -> bool:
    """Whether the repository the project's .ai-factory lies in holds at least
    one path beneath it at the commit the run is at, in whichever repository
    it lies — a link's folder and a repository of its own among them — which is
    what his clone, built from commits, holds."""
    try:
        listed = run_git_sync(
            ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
            cwd=project_dir / ".ai-factory",
        )
    except OSError:
        return False
    return listed.returncode == 0 and any(listed.stdout.split("\0"))


def _implementer_result(
    config: OrchestratorConfig,
    project_dir: Path,
    mode: Mode,
    tree: tuple[Repository, ...],
) -> ImplementerResultProtocol:
    """Choose how the implementer's result reaches the run's tree, by its runtime.

    An implementer on Hermes works in his own clone: his result is a pushed
    branch in every repository of the tree the loops found, laid as each
    repository's tree outside the run's own artifact paths for the roadmap in
    play. His clone holds only what commits hold, so where the repository
    .ai-factory lies in tracks none of it — a folder git ignores or does not
    track, in the project's repository or in the other, or an .ai-factory in
    no repository — it cannot have him as its implementer; a link and a
    repository of its own that track it can.
    """
    entry = role_runtimes(config.runtime, config.roles)["implementer"]
    if entry["runtime"] != "hermes":
        return WorkingTreeResult()
    if not _factory_tracked(project_dir):
        raise SystemExit(
            "Invalid 'roles' role 'implementer': the repository .ai-factory lies in "
            "tracks none of it, so his clone holds none of it"
        )
    preserved = tuple(_artifact_dir(project_dir, mode, name) for name in _IMPLEMENTER_RESULT_DIRS)
    return HermesResult(project_dir, tree, preserved)


def _print_run_choices(config: OrchestratorConfig) -> None:
    """Print the stages the run runs after the agent lines: planning, and the plan-review budget when planning is on."""
    print(f"Planning: {'on' if config.planning else 'off'}")
    if config.planning:
        print(f"Plan review iterations: {plan_review_budget(config.max_iterations, config.plan_review_iterations)}")


def _check_heads_reachable(identities: Sequence[HeadIdentity]) -> None:
    """Check that the answer command can run and read each head's address fresh, refusing as `HeadNotReachedError`."""
    if not identities:
        return
    command = answer_command()
    if not (command.is_file() and os.access(command, os.X_OK)):
        raise HeadNotReachedError(
            "A head could not be reached\n"
            f"The answer command {command} does not exist or is not executable"
        )
    for identity in identities:
        try:
            read_address(identity)  # a head's address is read fresh, before any backend is built
        except LocatorFault as fault:
            raise HeadNotReachedError(
                "A head could not be reached\n"
                f"{identity.repository} {identity.owner} {identity.folder}\n{fault}"
            ) from None


def _make_runtime(config: OrchestratorConfig, project_dir: Path, roles: tuple[AgentRole, ...], tree: tuple[Repository, ...]) -> tuple[AgentBackendProtocol, Callable[[], None]]:
    """Construct one backend per runtime a role of the run takes and bind the pre-task usage policy.

    The run's tree is handed to the Hermes backend, whose call checks and names
    each repository of it, as it is handed to the flow."""
    print(f"Agent runtime: {config.runtime}")
    runtimes = role_runtimes(config.runtime, config.roles, roles)
    taken = {entry["runtime"] for entry in runtimes.values()}
    heads: dict[str, HeadIdentity] = {}
    for entry in runtimes.values():
        if entry["runtime"] == "head":
            head = entry["head"]
            heads[backend_key(entry)] = _head_identity(head)
    _check_heads_reachable([identity for _, identity in sorted(heads.items())])
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
            backends[key] = HermesAgentBackend(settings["server"], settings["port"], settings["key_file"], tree)
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


def _test_loop(project_dir: Path, config: OrchestratorConfig) -> str | None:
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
    tree = find_run_tree(project_dir)
    backend, usage_check = _make_runtime(config, project_dir, mode.roles, tree)
    implementer_result = _implementer_result(config, project_dir, mode, tree)
    _print_run_choices(config)
    return _run_dynamic_loop(
        project_dir, roadmap_path, config,
        lambda m, i, sid: process_task(project_dir, m, i, config, mode, phase_session_id=sid, backend=backend, implementer_result=implementer_result, tree=tree),
        mode=mode,
        usage_check=usage_check,
    )


def _implement_loop(
    project_dir: Path, config: OrchestratorConfig, planner_prompt_name: str = "planner", roadmap_relpath: str | None = None,
    *, stop_for_closure: bool = False,
) -> str | None:
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
    tree = find_run_tree(project_dir)
    backend, usage_check = _make_runtime(config, project_dir, mode.roles, tree)
    implementer_result = _implementer_result(config, project_dir, mode, tree)
    _print_run_choices(config)
    closure_owed = (lambda: owed_phase(project_dir, relpath) is not None) if stop_for_closure else None
    return _run_dynamic_loop(
        project_dir, roadmap_path, config,
        lambda m, i, sid: process_task(project_dir, m, i, config, mode, phase_session_id=sid, backend=backend, implementer_result=implementer_result, tree=tree),
        mode=mode,
        usage_check=usage_check,
        closure_owed=closure_owed,
    )


class _WalkLoop(NamedTuple):
    """A walk value's loop, by the name it has in this module, and whether the
    loop takes the closure check."""

    loop_name: str
    closure_check: bool


_WALK_LOOPS: dict[str, _WalkLoop] = {
    "implement": _WalkLoop("_implement_loop", True),
    "test": _WalkLoop("_test_loop", False),
}


def _walk_stops_for_closure(value: str, options: Mapping[str, str]) -> bool:
    """Whether a walk of `value` with `options` stops before a task for a phase
    that owes a closure: only a loop that takes the closure check does."""
    return _WALK_LOOPS[value].closure_check and options.get("stop") == CLOSURE_OWED


def _walk_outcomes(step: Step) -> frozenset[str]:
    if _walk_stops_for_closure(step.value, step.options):
        return frozenset({NO_TASK, CLOSURE_OWED})
    return frozenset({NO_TASK})


def _close_outcomes(step: Step) -> frozenset[str]:
    return frozenset({DONE})


class _KindInputs(NamedTuple):
    """What the run holds when it builds a step kind's callable."""

    project_dir: Path
    config: OrchestratorConfig
    template: Path | None
    heads: Mapping[str, tuple[HeadIdentity, HeadAgentBackend]]


class _ScenarioKind(NamedTuple):
    """A step kind held once: the declaration the loader reads and the builder
    of the callable the executor runs."""

    step_kind: StepKind
    build: Callable[[_KindInputs], StepKindProtocol]


def _walk(project_dir: Path, config: OrchestratorConfig, value: str, options: Mapping[str, str]) -> str:
    """One walk step: run the loop of the mode the step's value names over the
    roadmap and return the outcome it ended in, ``no-task`` or
    ``closure-owed``.

    The step's ``stop`` option set to ``closure-owed`` declares the stop before
    a task for a phase that owes a closure; only the implement loop takes it.
    A person's stop still ends the scenario, raised before an outcome is read."""
    record = _WALK_LOOPS[value]
    loop = globals()[record.loop_name]
    if record.closure_check:
        outcome = loop(project_dir, config, stop_for_closure=_walk_stops_for_closure(value, options))
    else:
        outcome = loop(project_dir, config)
    if state.stop_requested:
        raise RunStoppedError("the person stopped the run")
    assert outcome is not None  # a loop ends without an outcome only at a person's stop
    return outcome


def _build_walk(inputs: _KindInputs) -> StepKindProtocol:
    return partial(_walk, inputs.project_dir, inputs.config)


def _build_close(inputs: _KindInputs) -> StepKindProtocol:
    # A close step implies close words, which imply a checked template.
    assert inputs.template is not None
    return CloseStep(
        project_dir=inputs.project_dir,
        relpath=_resolve_roadmap_relpath(inputs.config, inputs.project_dir),
        template=inputs.template,
        heads=inputs.heads,
    )


_SCENARIO_KIND_RECORDS: dict[str, _ScenarioKind] = {
    "walk": _ScenarioKind(
        step_kind=StepKind(
            values=tuple(_WALK_LOOPS),
            options={"stop": StepOption(values=(CLOSURE_OWED,), required=False)},
            outcomes=_walk_outcomes,
        ),
        build=_build_walk,
    ),
    "close": _ScenarioKind(
        step_kind=StepKind(
            values=None,
            options={"peer": StepOption(values=None, required=True)},
            outcomes=_close_outcomes,
        ),
        build=_build_close,
    ),
}

_SCENARIO_KINDS = {key: record.step_kind for key, record in _SCENARIO_KIND_RECORDS.items()}


def _is_closure_walk(step: Step) -> bool:
    """Whether `step` is a walk that declares the stop before a phase that owes a closure."""
    return step.kind == "walk" and step.options.get("stop") == CLOSURE_OWED


def _check_stop_pairs(path: Path, scenario: Scenario) -> None:
    """Refuse a declared `closure-owed` stop that no `close` step follows in its
    cycle, or that stands in no cycle at all."""
    for i, item in enumerate(scenario.steps):
        if isinstance(item, Cycle):
            for j, member in enumerate(item.steps):
                if _is_closure_walk(member) and not any(
                    later.kind == "close" for later in item.steps[j + 1:]
                ):
                    raise refusal(
                        path,
                        f"steps[{i}].cycle.steps[{j}].stop: {CLOSURE_OWED!r} "
                        "with no 'close' step after it in its cycle",
                    )
        elif _is_closure_walk(item):
            raise refusal(
                path,
                f"steps[{i}].stop: {CLOSURE_OWED!r} declared by a walk that stands in no cycle",
            )


def _close_words(scenario: Scenario) -> list[tuple[str, str]]:
    """The `(where, word)` of every head word a `close` step names, its value
    and its `peer`, inside cycles too; empty when the scenario holds no close step."""
    words: list[tuple[str, str]] = []

    def collect(step: Step, where: str) -> None:
        if step.kind == "close":
            words.append((f"{where}.close", step.value))
            words.append((f"{where}.peer", step.options["peer"]))

    for i, item in enumerate(scenario.steps):
        if isinstance(item, Cycle):
            for j, member in enumerate(item.steps):
                collect(member, f"steps[{i}].cycle.steps[{j}]")
        else:
            collect(item, f"steps[{i}]")
    return words


def _held_kinds(scenario: Scenario) -> set[str]:
    """The `kind` of every step the scenario holds, inside cycles too."""
    kinds: set[str] = set()
    for item in scenario.steps:
        if isinstance(item, Cycle):
            kinds.update(member.kind for member in item.steps)
        else:
            kinds.add(item.kind)
    return kinds


def _check_close_words(path: Path, words: Sequence[tuple[str, str]], heads: Mapping[str, Mapping[str, str]]) -> None:
    """Refuse the first head word `heads` does not map."""
    for where, word in words:
        if word not in heads:
            raise refusal(path, f"{where}: {word!r} is not mapped by 'heads'")


def _head_identity(address: Mapping[str, str]) -> HeadIdentity:
    """The identity of the head a `heads` entry addresses."""
    return HeadIdentity(Path(address["repository"]), address["owner"], address["folder"])


def _run_lifecycle(
    project_dir: Path,
    config: OrchestratorConfig,
    loop: Callable[[Path, OrchestratorConfig], None],
    banner: str,
) -> None:
    """The run's lifecycle: reset the run's state, install the interrupt
    handler, run the loop under the keep-awake wrapper and print the closing
    banner with the elapsed time."""
    state.queue_exhausted = False
    state.config = config
    state.project_dir = project_dir
    state.run_started = time.monotonic()
    state.tasks_done = 0
    signal.signal(signal.SIGINT, _handle_sigint)
    time_str = _with_caffeinate(loop, project_dir, config)
    print(f"\n{'='*60}")
    print(f"{banner} DONE — {time_str}")
    print(f"{'='*60}")


def _run_outcome_chain(
    config: OrchestratorConfig,
    project_dir: Path,
    run: Callable[[], None],
) -> bool:
    """Call `run` and map its exceptions to the run's outcomes: each of a stop,
    a halt and an escalation prints its frame, reports and exits with status 0;
    a walk the person stopped (`RunStoppedError`) exits with status 0, prints
    no frame and sends no report of its own: `_with_caffeinate` has printed
    `>>> Ran for … before stopping.` before it, and the loop has reported the
    stop except at its empty-queue early return;
    any other exception is reported as an error and re-raised. Returns True
    when `run` returns."""
    try:
        run()
        return True
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
    except RunStoppedError:
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


def run_implement(project_dir: Path, config: OrchestratorConfig) -> None:
    """Implement only — plan + implement tasks, no review pass."""
    _run_lifecycle(project_dir, config, _implement_loop, "IMPLEMENT")


def run_test(project_dir: Path, config: OrchestratorConfig) -> None:
    """Test mode — plan + implement tests, gate on real test runner output."""
    _run_lifecycle(project_dir, config, _test_loop, "TEST")


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
        ("run", "Run a declared scenario of steps"),
    ]:
        p = subparsers.add_parser(cmd, help=help_text)
        if cmd == "run":
            p.add_argument("scenario", help="The scenario's name")
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
    show_parser.add_argument("task", nargs="?", help=_TASK_HELP)
    show_parser.add_argument("--phase", help="The phase's number")

    rollback_parser = subparsers.add_parser("rollback", help="Take a task back to a position show listed for it")
    rollback_parser.error = _refuse_usage
    rollback_parser.add_argument("--project-dir", help="Path to the project directory")
    rollback_parser.add_argument("--roadmap", help="The roadmap to read, as the run's roadmap_path takes it")
    rollback_parser.add_argument("task", help=_TASK_HELP)
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

    if args.command == "dispose":
        # The command answers from the run's data alone and touches none of
        # the run's state, handlers or loops: it loads no configuration and
        # reads no roadmap — the deposit on standard input is its own data,
        # the dispositions record alone is what it writes, and no target the
        # deposit names is opened.
        try:
            dispose(project_dir)
        except (RunDataRefusal, HaltError, SystemExit) as e:
            refuse(e)
        return

    if args.command in ("show", "rollback", "sweep"):
        # The commands answer from the run's data alone: they load the
        # configuration as a run does and touch none of the run's state,
        # handlers or loops. A rollback changes the run's records, artifacts
        # and working tree — the head's exchange files among the things it
        # leaves where they stand — and a sweep removes the run's artifacts
        # of a finished phase alone.
        try:
            config = load_config(project_dir=project_dir, user_slug=_run_settings_slug(project_dir))
            if args.command == "show":
                show(config, project_dir, roadmap=args.roadmap, task=args.task, phase=args.phase)
            elif args.command == "rollback":
                rollback(config, project_dir, roadmap=args.roadmap, task=args.task, position=args.position)
            else:
                sweep(config, project_dir, roadmap=args.roadmap, phase=args.phase)
        except (RunDataRefusal, HaltError, SystemExit) as e:
            refuse(e)
        return

    completed_normally = False
    try:
        if args.command == "run":
            # A refused scenario starts nothing: the load, the stop pairs and
            # the closure template are refused before the configuration is
            # loaded, and no runtime is built. A scenario with no close step
            # reads no template.
            template = None
            try:
                resolved = resolve_scenario(args.scenario, project_dir)
                scenario = load_scenario(resolved.path, _SCENARIO_KINDS)
                _check_stop_pairs(resolved.path, scenario)
                close_words = _close_words(scenario)
                if close_words:
                    template = resolve_template(project_dir)
                    check_template(template)
            except ScenarioRefusal as e:
                refuse(e)
            if resolved.shadows:
                print(f"Scenario {args.scenario!r}: the project's {resolved.path} runs in place of the shipped one")
        config = load_config(project_dir=project_dir, user_slug=_run_settings_slug(project_dir))

        if args.command == "run":
            heads: dict[str, tuple[HeadIdentity, HeadAgentBackend]] = {}
            if close_words:
                # The one scenario refusal that follows the configuration: the
                # heads a close step names are the person's, and it comes
                # before any runtime is built.
                try:
                    _check_close_words(resolved.path, close_words, config.heads)
                except ScenarioRefusal as e:
                    refuse(e)
                for word in sorted({word for _, word in close_words}):
                    identity = _head_identity(config.heads[word])
                    heads[word] = (identity, HeadAgentBackend(identity))

            def scenario_loop(p: Path, c: OrchestratorConfig) -> None:
                # The heads a close step names are reached before any step runs.
                _check_heads_reachable([identity for identity, _ in heads.values()])
                # One callable per kind the scenario holds, built from the
                # kinds' records: a scenario with no close step builds no close
                # callable. The close step resolves the roadmap here, inside
                # the run, so a halt there ends like any other halt.
                inputs = _KindInputs(project_dir, config, template, heads)
                kinds: dict[str, StepKindProtocol] = {
                    kind: _SCENARIO_KIND_RECORDS[kind].build(inputs) for kind in _held_kinds(scenario)
                }
                run_scenario(scenario, kinds)

            run = lambda: _run_lifecycle(project_dir, config, scenario_loop, "SCENARIO")
        elif args.command == "test":
            run = lambda: run_test(project_dir, config)
        else:
            run = lambda: run_implement(project_dir, config)
        completed_normally = _run_outcome_chain(config, project_dir, run)
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

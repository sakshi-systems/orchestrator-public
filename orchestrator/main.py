"""Composition root and CLI: assemble the mode for a run and hand it to the flow."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Callable

from .claude_cli import ClaudeAgentBackend
from .codex_cli import CodexAgentBackend
from .config import OrchestratorConfig, load_config
from .errors import EscalationError, HaltError, PipelineStopError
from .layout import _artifact_subdir, _external_evidence_path, _resolve_roadmap_relpath, _tests_sibling
from .notify import Outcome, report
from .pipeline import IMPLEMENT_MODE, TEST_MODE, _run_dynamic_loop, process_task
from .ports import AgentBackendProtocol
from .runtime import _handle_sigint, _run_summary, _with_caffeinate
from .usage import _check_codex_usage_limits, _check_usage_limits
from . import state


def _make_runtime(config: OrchestratorConfig, project_dir: Path) -> tuple[AgentBackendProtocol, Callable[[], None]]:
    """Construct the selected backend and bind its pre-task usage policy."""
    print(f"Agent runtime: {config.runtime}")
    if config.runtime == "codex":
        backend = CodexAgentBackend()
        return backend, partial(_check_codex_usage_limits, config, project_dir)
    backend = ClaudeAgentBackend()
    return backend, partial(_check_usage_limits, config)


def _test_loop(project_dir: Path, config: OrchestratorConfig) -> None:
    """Write tests for all pending tasks from the roadmap's test sibling."""
    main_relpath = _resolve_roadmap_relpath(config, project_dir)
    relpath = _tests_sibling(main_relpath)
    roadmap_path = project_dir / ".ai-factory" / relpath
    if not roadmap_path.exists():
        print(f"ERROR: No roadmap found at {roadmap_path}")
        sys.exit(1)
    mode = TEST_MODE._replace(roadmap_relpath=relpath, artifact_subdir=_artifact_subdir(relpath))
    backend, usage_check = _make_runtime(config, project_dir)
    _run_dynamic_loop(
        project_dir, roadmap_path, config,
        lambda m, i, sid: process_task(project_dir, m, i, config, mode, phase_session_id=sid, backend=backend),
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
    mode = IMPLEMENT_MODE._replace(planner_prompt_name=planner_prompt_name, roadmap_relpath=relpath, artifact_subdir=_artifact_subdir(relpath))
    backend, usage_check = _make_runtime(config, project_dir)
    _run_dynamic_loop(
        project_dir, roadmap_path, config,
        lambda m, i, sid: process_task(project_dir, m, i, config, mode, phase_session_id=sid, backend=backend),
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

    args = parser.parse_args()
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

    completed_normally = False
    try:
        config = load_config(project_dir=project_dir)

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

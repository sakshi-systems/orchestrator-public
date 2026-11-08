"""Claude CLI adapter — ClaudeAgentBackend and its native role policy, resolving and launching the claude CLI, classifying results, and retries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from . import state
from .errors import NetworkError, RateLimitError
from .ports import AgentRole
from .process_control import kill_active_child


MAX_RETRIES = 3
RETRY_DELAY = 30  # seconds


def _is_overloaded(result_text: str) -> bool:
    """True if result_text names an API overload."""
    return "overloaded" in result_text.lower() or "529" in result_text


_TRANSPORT_MARKERS = (
    "connection closed mid-response",
    "connection error",
    "connection reset",
    "econnreset",
    "fetch failed",
    "socket hang up",
)


def _is_transport_fault(result_text: str) -> bool:
    """True if result_text names a transport failure, case-insensitively, as a substring."""
    lowered = result_text.lower()
    return any(marker in lowered for marker in _TRANSPORT_MARKERS)


def _classify_result(parsed_final: dict, result_text: str, returncode: int,
                     is_error: bool, attempt: int, max_retries: int) -> str:
    """Classify a finished Claude CLI invocation into the terminal action
    `_run_claude` should take.

    Returns one of the literals `"retry" | "ratelimit" | "network_halt" |
    "error" | "ok"`. `no_result = not parsed_final` means the CLI exited
    before emitting any `result` event — an infra/network death, not a task
    outcome — as opposed to a result-bearing nonzero exit, which is a
    task-level outcome only when its text names no transport fault, or an
    `is_error` result on a zero exit, which is a task-level outcome
    whatever its text names.

    """
    no_result = not parsed_final

    if _is_overloaded(result_text) and attempt < max_retries:
        return "retry"
    if _is_transport_fault(result_text) and returncode != 0 and attempt < max_retries:
        return "retry"
    if _is_transport_fault(result_text) and returncode != 0:
        return "network_halt"
    if no_result and returncode != 0 and attempt < max_retries:
        return "retry"
    if no_result and returncode != 0:
        return "network_halt"
    if returncode != 0 and ("hit your limit" in result_text.lower() or "resets" in result_text.lower()):
        return "ratelimit"
    if returncode != 0:
        return "error"
    if is_error and ("hit your limit" in result_text.lower() or "resets" in result_text.lower()):
        return "ratelimit"
    if is_error:
        return "error"
    return "ok"


def _sorted_nvm_node_dirs(dirs: list[Path]) -> list[Path]:
    """Sort nvm version directories highest-version-first, semver-aware.

    Parses each `Path.name` by stripping a leading "v" and taking the
    leading run of all-decimal ("." separated) segments into an int tuple
    (e.g. "v18.20.4" -> (18, 20, 4)). Directories with at least one
    parseable leading segment always outrank ones with none; unparseable
    names (e.g. "system", "lts", a stray file) sort last, ordered among
    themselves by their string name. Never raises, including on an empty
    list or names with mixed/trailing non-integer segments.
    """

    def parse_version(d: Path) -> tuple[int, ...] | None:
        name = d.name[1:] if d.name.startswith("v") else d.name
        version: list[int] = []
        for segment in name.split("."):
            if not segment.isdecimal():
                break
            version.append(int(segment))
        return tuple(version) if version else None

    parseable: list[tuple[tuple[int, ...], Path]] = []
    unparseable: list[Path] = []
    for d in dirs:
        version = parse_version(d)
        if version is not None:
            parseable.append((version, d))
        else:
            unparseable.append(d)

    parseable.sort(key=lambda pair: pair[0], reverse=True)
    unparseable.sort(key=lambda d: d.name)
    return [d for _, d in parseable] + unparseable


def _resolve_claude() -> str:
    """Return the absolute path to the claude CLI, or raise if not found."""
    path = shutil.which("claude")
    if path:
        return path
    # nvm installs into a versioned directory that may not be in a subprocess PATH
    nvm_base = Path.home() / ".nvm" / "versions" / "node"
    if nvm_base.is_dir():
        for node_dir in _sorted_nvm_node_dirs(list(nvm_base.iterdir())):
            candidate = node_dir / "bin" / "claude"
            if candidate.exists():
                return str(candidate)
    raise FileNotFoundError(
        "claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
    )


_CLAUDE_BIN: str | None = None


def _run_claude(
    prompt: str,
    cwd: str,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[str, str]:
    """Run claude CLI and return (output_text, session_id)."""
    global _CLAUDE_BIN
    if _CLAUDE_BIN is None:
        _CLAUDE_BIN = _resolve_claude()

    if allowed_tools is None:
        allowed_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

    cmd = [
        _CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--allowedTools", ",".join(allowed_tools),
        "--disallowedTools", "Monitor,ScheduleWakeup,CronCreate",
    ]

    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])

    if session_id:
        cmd.extend(["--resume", session_id])
    elif system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    env = {**os.environ, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n--- Claude agent ({cwd}) ---")
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        state.active_proc = proc

        lines: list[str] = []
        sid_seen: str = ""

        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                lines.append(line)
                if not sid_seen and line:
                    try:
                        event = json.loads(line)
                        s = event.get("session_id", "")
                        if s:
                            sid_seen = s
                            print(f"  [session: {sid_seen}]")
                    except json.JSONDecodeError:
                        pass
        except KeyboardInterrupt:
            kill_active_child()
            if sid_seen:
                print(f"\n>>> Interrupted — session: {sid_seen}")
            else:
                print("\n>>> Interrupted (session_id not yet received).")
            sys.exit(130)

        proc.wait()
        state.active_proc = None
        stdout = "\n".join(lines)

        elapsed = int(time.monotonic() - start)
        mins, secs = divmod(elapsed, 60)

        # Find the final result event (last line with "result" key)
        parsed_final: dict = {}
        for line in reversed(lines):
            if not line:
                continue
            try:
                ev = json.loads(line)
                if "result" in ev:
                    parsed_final = ev
                    break
            except json.JSONDecodeError:
                continue

        result_text = parsed_final.get("result", "")
        is_error = parsed_final.get("is_error", False)

        verdict = _classify_result(parsed_final, result_text, proc.returncode, is_error, attempt, MAX_RETRIES)

        if verdict == "retry":
            if not parsed_final:
                print(f">>> Network error / no result event, retrying in {RETRY_DELAY}s (attempt {attempt}/{MAX_RETRIES})...")
            elif _is_overloaded(result_text):
                print(f">>> API overloaded, retrying in {RETRY_DELAY}s (attempt {attempt}/{MAX_RETRIES})...")
            else:
                print(f">>> Transport error, retrying in {RETRY_DELAY}s (attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(RETRY_DELAY)
            continue

        if verdict == "network_halt":
            if not parsed_final:
                raise NetworkError(
                    f"Claude CLI died with no result event, exit code {proc.returncode}\n"
                    f"stdout: {stdout if stdout else '(empty)'}"
                )
            raise NetworkError(
                f"Claude CLI reported a transport failure, exit code {proc.returncode}\n"
                f"result: {result_text[:500]}"
            )

        if verdict == "ratelimit":
            raise RateLimitError(f"Agent reported a rate limit\n{result_text}")

        if verdict == "error":
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Claude CLI failed with exit code {proc.returncode}\n"
                    f"stdout: {stdout if stdout else '(empty)'}"
                )
            raise RuntimeError(f"Claude returned error: {result_text[:500]}")

        # verdict == "ok"
        if not lines:
            raise RuntimeError(
                "Claude CLI exited 0 but stdout is empty"
            )

        sid = parsed_final.get("session_id", sid_seen)
        if not sid_seen:
            # session_id only in final event — print it now
            print(f"  [session: {sid}]")

        summary = result_text[:500] + ("..." if len(result_text) > 500 else "")
        print(f"{summary}\n  [{mins}m {secs}s]")

        return result_text, sid

    raise RuntimeError("All retry attempts exhausted")


class _NativePolicy(NamedTuple):
    model: str
    effort: str
    allowed_tools: tuple[str, ...]


_ROLE_POLICY: dict[AgentRole, _NativePolicy] = {
    AgentRole.PLANNER_REVIEWER: _NativePolicy("opus", "high", ("Read", "Write", "Glob", "Grep", "Bash")),
    AgentRole.PLAN_REVIEWER: _NativePolicy("opus", "high", ("Read", "Write", "Glob", "Grep", "Bash")),
    AgentRole.IMPLEMENTER: _NativePolicy("sonnet", "high", ("Read", "Write", "Edit", "Bash", "Glob", "Grep")),
}


class ClaudeAgentBackend:
    """Claude implementation of the agent-runtime port.

    Chooses model, effort and tools from the role alone, and delivers the
    instructions as the system prompt only on a fresh session — a resumed
    session already carries them. Construction validates that the executable
    is available, without invoking it.
    """

    def __init__(self) -> None:
        global _CLAUDE_BIN
        if _CLAUDE_BIN is None:
            _CLAUDE_BIN = _resolve_claude()

    def run(
        self,
        *,
        role: AgentRole,
        prompt: str,
        cwd: str,
        instructions: str,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        policy = _ROLE_POLICY[role]
        return _run_claude(
            prompt=prompt,
            cwd=cwd,
            system_prompt=instructions if not session_id else None,
            session_id=session_id,
            allowed_tools=list(policy.allowed_tools),
            model=policy.model,
            effort=policy.effort,
        )


def _run_usage_probe() -> str:
    """Run the resolved claude CLI's /usage command and return its stdout."""
    result = subprocess.run([_resolve_claude(), "/usage"], capture_output=True, text=True, timeout=30)
    return result.stdout

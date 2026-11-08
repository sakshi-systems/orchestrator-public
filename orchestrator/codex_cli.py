"""Codex CLI adapter — CodexAgentBackend, the Codex implementation of the agent-runtime port.

Owns the native argument vectors, the TOML encoding of the initial role
instructions, JSON-Lines event parsing, narrow failure recognition and the
bounded retries that keep thread identity. Assembly pairs this backend with
its account quota policy.
"""

from __future__ import annotations

import codecs
import json
import os
import queue
import re
import shutil
import string
import subprocess
import threading
import time
import uuid
from typing import Any, NamedTuple

from . import state
from .errors import NetworkError, RateLimitError
from .ports import AgentRole
from .process_control import kill_active_child


MAX_ATTEMPTS = 3
RETRY_DELAY = 30  # seconds

_CALLER_IDENTITY_VARS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID")


class _NativePolicy(NamedTuple):
    model: str
    effort: str


_ROLE_POLICY: dict[AgentRole, _NativePolicy] = {
    AgentRole.PLANNER_REVIEWER: _NativePolicy("gpt-6.1-sol", "high"),
    AgentRole.PLAN_REVIEWER: _NativePolicy("gpt-6.1-sol", "high"),
    AgentRole.IMPLEMENTER: _NativePolicy("gpt-6.1-sol", "high"),
}


def _resolve_codex() -> str:
    """Return the path to the codex CLI, or raise if it is not on PATH."""
    path = shutil.which("codex")
    if path:
        return path
    raise FileNotFoundError(
        "Codex CLI not found\n"
        "Install it with: npm install -g @openai/codex"
    )


# ---------------------------------------------------------------------------
# Invocation forms
# ---------------------------------------------------------------------------

_TOML_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_basic_string(text: str) -> str:
    """Encode text as a TOML basic string, preserving every Unicode code point literally."""
    parts: list[str] = ['"']
    for ch in text:
        code = ord(ch)
        short = _TOML_SHORT_ESCAPES.get(ch)
        if short is not None:
            parts.append(short)
        elif code < 0x20 or code == 0x7F:
            parts.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError(f"lone surrogate U+{code:04X} cannot be represented in TOML")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def _fresh_argv(bin_path: str, policy: _NativePolicy, cwd: str, instructions: str) -> list[str]:
    """Argument vector creating a new thread with the instructions in the developer layer."""
    return [
        bin_path, "exec", "--json", "--color", "never",
        "-m", policy.model,
        "-c", f'model_reasoning_effort="{policy.effort}"',
        "-c", 'approval_policy="never"',
        "-s", "danger-full-access",
        "-C", cwd,
        "-c", "developer_instructions=" + _toml_basic_string(instructions),
        "-",
    ]


def _resume_argv(bin_path: str, policy: _NativePolicy, cwd: str, thread_id: str) -> list[str]:
    """Argument vector continuing a persisted thread; the initial developer policy is retained natively."""
    return [
        bin_path, "exec",
        "-C", cwd,
        "-s", "danger-full-access",
        "-c", 'approval_policy="never"',
        "resume", "--json",
        "-m", policy.model,
        "-c", f'model_reasoning_effort="{policy.effort}"',
        thread_id,
        "-",
    ]


def _child_env() -> dict[str, str]:
    """The caller's environment without the identity of any enclosing Codex session."""
    return {k: v for k, v in os.environ.items() if k not in _CALLER_IDENTITY_VARS}


def _read_rate_limits(cwd: str) -> dict[str, Any]:
    """Read the correlated account result through one bounded native dialogue."""
    deadline = time.monotonic() + 30

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Codex quota query exceeded its 30-second deadline")
        return budget

    proc = subprocess.Popen(
        [_resolve_codex(), "app-server", "--stdio"], cwd=cwd,
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    state.active_proc = proc
    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    workers: list[threading.Thread] = []
    finished: set[str] = set()
    cancelled = threading.Event()

    def drain(pipe: Any, name: str) -> None:
        try:
            descriptor = pipe.fileno()
            os.set_blocking(descriptor, False)
            decoder = codecs.getincrementaldecoder("utf-8")()
            pending = ""
            while not cancelled.is_set():
                try:
                    data = os.read(descriptor, 65536)
                except BlockingIOError:
                    cancelled.wait(0.01)
                    continue
                text = decoder.decode(data, final=not data)
                if name == "stdout":
                    lines = (pending + text).split("\n")
                    pending = lines.pop()
                    for line in lines:
                        events.put(("line", line))
                if not data:
                    if name == "stdout" and pending:
                        events.put(("line", pending))
                    break
        except BaseException as exc:
            events.put(("error", exc))
        finally:
            events.put(("eof", name))

    def send(frame: dict[str, Any]) -> None:
        remaining()
        # All three fixed requests fit in the empty stdin pipe; concurrent
        # readers prevent output backpressure from blocking this dialogue.
        proc.stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        remaining()

    def receive() -> dict[str, Any] | None:
        try:
            kind, value = events.get(timeout=remaining())
        except queue.Empty as exc:
            raise TimeoutError("Codex quota query exceeded its 30-second deadline") from exc
        # A queued response may itself have consumed the remaining budget.
        remaining()
        if kind == "error":
            raise value
        if kind == "eof":
            finished.add(value)
            return None
        try:
            frame = json.loads(value)
        except ValueError as exc:
            raise RuntimeError("Codex quota response is not valid JSON") from exc
        if not isinstance(frame, dict):
            raise RuntimeError("Codex quota response is not a JSON object")
        if type(frame.get("id")) is int and frame["id"] in (1, 2):
            if "error" in frame:
                raise RuntimeError("Codex quota request returned an RPC error")
            if not isinstance(frame.get("result"), dict):
                raise RuntimeError("Codex quota response has no object result")
        return frame

    def reply(request_id: int) -> dict[str, Any]:
        while "stdout" not in finished:
            frame = receive()
            if frame is not None and type(frame.get("id")) is int and frame["id"] == request_id:
                return frame["result"]
        raise RuntimeError("Codex quota output ended before the requested reply")

    try:
        remaining()
        for name in ("stdout", "stderr"):
            worker = threading.Thread(target=drain, args=(getattr(proc, name), name))
            workers.append(worker)
            worker.start()
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "orchestrator", "version": "0.1.0"}}})
        reply(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        result = reply(2)
        proc.stdin.close()
        remaining()
        while len(finished) < 2:
            receive()
        for worker in workers:
            worker.join(timeout=remaining())
            remaining()
            if worker.is_alive():
                raise TimeoutError("Codex quota reader did not finish before the deadline")
        returncode = proc.wait(timeout=remaining())
        remaining()
        if returncode != 0:
            raise RuntimeError("Codex quota query exited unsuccessfully")
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
        remaining()
        return result
    except BaseException:
        # Retain the local child even if shared force-quit already cleared it.
        # Nonblocking readers stop even if an exited child's descendant still
        # owns an output writer. Finish them before the reaper closes pipes.
        cancelled.set()
        for worker in workers:
            if worker.ident is not None:
                while worker.is_alive():
                    try:
                        worker.join()
                    except BaseException:
                        pass
        # Cleanup must not replace an original I/O error or interruption.
        try:
            _terminate_and_reap(proc)
        except BaseException:
            pass
        raise
    finally:
        if state.active_proc is proc:
            state.active_proc = None


# ---------------------------------------------------------------------------
# Child process ownership
# ---------------------------------------------------------------------------


class _Completed(NamedTuple):
    stdout: str
    stderr: str
    returncode: int


def _terminate_and_reap(proc: subprocess.Popen) -> None:
    """Kill a live child through shared cleanup, reap it and close its pipes; never raises."""
    try:
        if proc.poll() is None:
            state.active_proc = proc
            kill_active_child()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass
    except Exception:
        pass


def _communicate_once(argv: list[str], cwd: str, prompt: str) -> _Completed:
    """Run one native attempt: deliver the assignment on stdin, buffer both streams, reap the child."""
    proc = subprocess.Popen(
        argv, cwd=cwd,
        env=_child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    state.active_proc = proc
    try:
        stdout, stderr = proc.communicate(input=prompt)
        return _Completed(stdout or "", stderr or "", proc.returncode)
    except BaseException:
        _terminate_and_reap(proc)
        raise
    finally:
        if state.active_proc is proc:
            state.active_proc = None


# ---------------------------------------------------------------------------
# Stream parsing
# ---------------------------------------------------------------------------


class _Stream(NamedTuple):
    thread_id: str | None
    last_message: str | None
    completed: bool
    failed_message: str | None
    last_error: str | None


class _ProtocolDefect(Exception):
    """A consumed native field is malformed or the terminal events contradict each other."""


def _is_canonical_uuid(value: object) -> bool:
    """True only for a string that is exactly the canonical text of a UUID (`str(uuid.UUID(value)) == value`)."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _parse_stream(stdout: str) -> _Stream:
    """Read the JSON-Lines event stream; records are delimited by newline only."""
    thread_id: str | None = None
    last_message: str | None = None
    completed = False
    failed_message: str | None = None
    last_error: str | None = None

    for raw in stdout.split("\n"):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise _ProtocolDefect(f"line is not valid JSON: {raw[:200]}") from exc
        if not isinstance(event, dict):
            raise _ProtocolDefect(f"line is not a JSON object: {raw[:200]}")
        kind = event.get("type")
        if not isinstance(kind, str):
            raise _ProtocolDefect(f"event has no string type: {raw[:200]}")

        if kind == "thread.started":
            value = event.get("thread_id")
            if not isinstance(value, str):
                raise _ProtocolDefect("thread.started carries no string thread_id")
            if not _is_canonical_uuid(value):
                raise _ProtocolDefect(f"thread_id is not a canonical UUID: {value[:100]}")
            if thread_id is not None and thread_id != value:
                raise _ProtocolDefect("stream carries two different thread identities")
            thread_id = value
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise _ProtocolDefect("item.completed carries no item with a string type")
            if item["type"] == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    raise _ProtocolDefect("agent_message text is not a string")
                last_message = text
        elif kind == "turn.completed":
            completed = True
        elif kind == "turn.failed":
            error = event.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                raise _ProtocolDefect("turn.failed carries no string error.message")
            failed_message = error["message"]
        elif kind == "error":
            message = event.get("message")
            if not isinstance(message, str):
                raise _ProtocolDefect("error event carries no string message")
            last_error = message

    if completed and failed_message is not None:
        raise _ProtocolDefect("stream carries both turn.completed and turn.failed")
    return _Stream(thread_id, last_message, completed, failed_message, last_error)


def _succeeded(run: _Completed, stream: _Stream, requested: str | None) -> bool:
    """True only for a clean exit, a completed turn, a usable identity and a non-empty final message."""
    if run.returncode != 0 or not stream.completed or stream.failed_message is not None:
        return False
    if stream.thread_id is None:
        return False
    if requested is not None and stream.thread_id != requested:
        return False
    return stream.last_message is not None and bool(stream.last_message.strip())


# ---------------------------------------------------------------------------
# Failure recognition
# ---------------------------------------------------------------------------

_ERROR_PREFIX = "error:"

_QUOTA_SENTENCES = (
    "your workspace is out of credits.",
    "you hit your spend cap set in your workspace.",
    "you hit your spend cap set by the owner of your workspace.",
)
_USAGE_LIMIT_OPENING = "you've hit your usage limit"
_QUOTA_COMPLETE = "quota exceeded. check your plan and billing details."

_TRANSIENT_COMPLETE = (
    "request timed out",
    "selected model is at capacity. please try a different model.",
)
_RETRY_LIMIT_FORM = re.compile(
    r"exceeded retry limit, last status: "
    r"(500 internal server error|502 bad gateway|503 service unavailable|504 gateway timeout)"
    r"(, request id: [^\r\n]+)?"
)


def _stderr_error_records(stderr: str) -> list[str]:
    """Diagnostics of every stderr line that begins with `Error:`; other lines are never searched."""
    records: list[str] = []
    for line in stderr.split("\n"):
        stripped = line.strip()
        if stripped[: len(_ERROR_PREFIX)].casefold() == _ERROR_PREFIX:
            records.append(stripped[len(_ERROR_PREFIX):])
    return records


def _normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").strip()
    if text[: len(_ERROR_PREFIX)].casefold() == _ERROR_PREFIX:
        text = text[len(_ERROR_PREFIX):].strip()
    return text.casefold()


def _is_quota(norm: str) -> bool:
    if norm.startswith(_USAGE_LIMIT_OPENING):
        rest = norm[len(_USAGE_LIMIT_OPENING):]
        if not rest or rest[0] in string.punctuation or rest.startswith(" for "):
            return True
    if any(norm.startswith(sentence) for sentence in _QUOTA_SENTENCES):
        return True
    return norm == _QUOTA_COMPLETE


def _is_transient(norm: str) -> bool:
    return norm in _TRANSIENT_COMPLETE or _RETRY_LIMIT_FORM.fullmatch(norm) is not None


def _classify_failure(stream: _Stream, stderr: str) -> tuple[str, str | None]:
    """Classify a failed or incomplete invocation as quota, transient or unknown, with its primary diagnostic."""
    records = _stderr_error_records(stderr)
    if stream.failed_message is not None:
        primary: str | None = stream.failed_message
    elif stream.last_error is not None:
        primary = stream.last_error
    elif records:
        primary = records[-1]
    else:
        primary = None

    if primary is None:
        return "unknown", None
    norm = _normalize(primary)
    if _is_quota(norm):
        return "quota", primary
    if not _is_transient(norm):
        return "unknown", primary

    auxiliaries = [_normalize(r) for r in records]
    if any(_is_quota(a) for a in auxiliaries):
        return "quota", primary
    if any(not _is_quota(a) and not _is_transient(a) for a in auxiliaries):
        return "unknown", primary
    return "transient", primary


def _evidence(primary: str | None, run: _Completed) -> str:
    return (
        f"primary diagnostic: {primary if primary else '(none)'}\n"
        f"exit status: {run.returncode}\n"
        f"stdout: {run.stdout if run.stdout else '(empty)'}\n"
        f"stderr: {run.stderr if run.stderr else '(empty)'}"
    )


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


class CodexAgentBackend:
    """Codex implementation of the agent-runtime port.

    Chooses model and effort from the role alone and delivers the instructions
    in the developer layer only when it creates a thread; a continuation keeps
    the initial policy natively. Construction resolves the executable without
    invoking a model.
    """

    def __init__(self) -> None:
        self._bin = _resolve_codex()

    def run(
        self,
        *,
        role: AgentRole,
        prompt: str,
        cwd: str,
        instructions: str,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        """Carry out the role's turn and return (final agent message, thread identity).

        A recognized transient failure is retried up to the attempt bound,
        resuming the thread the first attempt created or the one the caller
        supplied. Exhausted usage raises `RateLimitError`, a transient that
        persists raises `NetworkError`, and any other failure or protocol
        defect raises `RuntimeError`. A failed invocation never returns.

        Only `session_id=None` creates a thread. A supplied value that is not
        canonical UUID text raises `ValueError` before any argument vector is
        built or any child is started; a canonical value is passed to native
        continuation unchanged.
        """
        policy = _ROLE_POLICY[role]
        if session_id is not None and not _is_canonical_uuid(session_id):
            raise ValueError(
                "Codex continuation identity is not a canonical UUID\n"
                f"supplied: {repr(session_id)[:100]}"
            )
        target = session_id

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"\n--- Codex agent ({cwd}) ---")
            start = time.monotonic()
            if target is not None:
                resumed_id: str | None = target
                argv = _resume_argv(self._bin, policy, cwd, target)
            else:
                resumed_id = None
                argv = _fresh_argv(self._bin, policy, cwd, instructions)

            run_ = _communicate_once(argv, cwd, prompt)

            try:
                stream = _parse_stream(run_.stdout)
            except _ProtocolDefect as defect:
                raise RuntimeError(
                    "Codex event stream is malformed\n"
                    f"defect: {defect}\n" + _evidence(None, run_)
                ) from defect

            if resumed_id is not None and stream.thread_id is not None and stream.thread_id != resumed_id:
                raise RuntimeError(
                    "Codex resumed a different thread\n"
                    f"requested: {resumed_id}\n"
                    f"emitted: {stream.thread_id}\n" + _evidence(None, run_)
                )

            if resumed_id is None and stream.thread_id is not None:
                target = stream.thread_id

            if _succeeded(run_, stream, resumed_id):
                elapsed = int(time.monotonic() - start)
                mins, secs = divmod(elapsed, 60)
                message = stream.last_message or ""
                print(f"  [session: {stream.thread_id}]")
                summary = message[:500] + ("..." if len(message) > 500 else "")
                print(f"{summary}\n  [{mins}m {secs}s]")
                return message, stream.thread_id  # type: ignore[return-value]

            verdict, primary = _classify_failure(stream, run_.stderr)
            if verdict == "quota":
                raise RateLimitError("Codex reported exhausted usage\n" + _evidence(primary, run_))
            if verdict == "unknown":
                raise RuntimeError("Codex invocation failed\n" + _evidence(primary, run_))
            if attempt == MAX_ATTEMPTS:
                raise NetworkError(
                    "Codex transient failure persisted through retries\n" + _evidence(primary, run_)
                )
            print(
                f">>> Codex transient failure, retrying in {RETRY_DELAY}s "
                f"(attempt {attempt}/{MAX_ATTEMPTS})..."
            )
            time.sleep(RETRY_DELAY)

        raise RuntimeError("All retry attempts exhausted")

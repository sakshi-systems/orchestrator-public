"""Offline coverage of the Codex adapter: child lifecycle, invocation policy, stream handling and fault recognition.

The lifecycle witnesses observe who owns the child, how it is terminated and
reaped on every exit path, and the identity and order of retried spawns. One
of them drives a real executable over real pipes to prove that a large
assignment and large output streams cannot deadlock the native turn.

The invocation-policy cases pin the argument-vector templates, the child
environment, executable resolution and the TOML encoding of the instructions.
The completion cases cover the event stream: the returned text, protocol
defects, incomplete invocations and the retry bound. The failure-recognition
cases cover quota, transient and unrecognized diagnostics, including how a
primary diagnostic and auxiliary stderr records interact. The supplied-identity
cases show that only an absent identity creates a thread and that a value which
is not canonical UUID text is rejected before any argument vector or child exists.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import codex_cli, process_control, state
from orchestrator.codex_cli import CodexAgentBackend
from orchestrator.errors import NetworkError, RateLimitError
from orchestrator.ports import AgentRole, RoleSetting

CREATED_UUID = "11111111-1111-4111-8111-111111111111"
SUPPLIED_UUID = "22222222-2222-4222-8222-222222222222"
FAKE_BIN = "/fake/bin/codex"
RETRY_DELAY_SECONDS = 30


# ---------------------------------------------------------------------------
# Native JSON-Lines frames
# ---------------------------------------------------------------------------


def _frame(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _thread_started(uuid: str) -> str:
    return _frame({"type": "thread.started", "thread_id": uuid})


def _agent_message(text: str) -> str:
    return _frame({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def _turn_completed() -> str:
    return _frame({"type": "turn.completed"})


def _turn_failed(message: str) -> str:
    return _frame({"type": "turn.failed", "error": {"message": message}})


def _error_event(message: str) -> str:
    return _frame({"type": "error", "message": message})


# ---------------------------------------------------------------------------
# Stateful process stand-in
# ---------------------------------------------------------------------------


class _ScriptedChild:
    """A Popen stand-in that records ownership and reaping, and refuses to block."""

    def __init__(self, pid, stdout_lines, stderr, returncode, effect, trace):
        self.pid = pid
        self._stdout_lines = stdout_lines
        self._stderr = stderr
        self._final_returncode = returncode
        self._effect = effect
        self._trace = trace
        self.killed = False
        self.finished = False
        self.reaped = False
        self.returncode = None
        self.owned_during_communicate: list[bool] = []
        self.inputs: list = []
        self.popen_kwargs: dict = {}
        self._communicated = False
        self.args: list = []
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self):
        if self.killed:
            return -9
        return self.returncode if self.finished else None

    def kill(self):
        self.killed = True
        self._trace.append("kill")

    terminate = kill

    def wait(self, timeout=None):
        if not (self.killed or self.finished):
            raise AssertionError("wait on a live child would block")
        self._reap()
        return self.returncode

    def _reap(self):
        self.reaped = True
        if self.returncode is None:
            self.returncode = -9 if self.killed else self._final_returncode
        self._trace.append("reap")

    def _text_mode(self) -> bool:
        kw = self.popen_kwargs
        return bool(kw.get("text") or kw.get("universal_newlines") or kw.get("encoding") or kw.get("errors"))

    def communicate(self, input=None, timeout=None):
        self.owned_during_communicate.append(state.active_proc is self)
        self.inputs.append(input.decode("utf-8") if isinstance(input, bytes) else input)
        first = not self._communicated
        self._communicated = True
        if first and self._effect is not None:
            effect, self._effect = self._effect, None
            if isinstance(effect, BaseException):
                raise effect
            effect()
        if not (self.killed or self.finished):
            if not first:
                raise AssertionError("communicate on a live child would block")
            self.finished = True
            self.returncode = self._final_returncode
            self._reap()
            return self._output(self._stdout_lines, self._stderr)
        self._reap()
        return self._output([], "")

    def _output(self, lines, stderr):
        out = "\n".join(lines) + "\n" if lines else ""
        if self._text_mode():
            return out, stderr
        return out.encode("utf-8"), stderr.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.wait()
        return False


class _SpawnRecorder:
    """The Popen factory: records argv and the lifecycle state at each spawn."""

    def __init__(self):
        self.queue: list[_ScriptedChild] = []
        self.spawned: list[_ScriptedChild] = []
        self.argvs: list[list[str]] = []
        self.spawn_checks: list[tuple[bool, bool]] = []

    def __call__(self, *args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        self.argvs.append(list(argv))
        self.spawn_checks.append((all(c.reaped for c in self.spawned), state.active_proc is None))
        if not self.queue:
            raise AssertionError("unscripted spawn")
        child = self.queue.pop(0)
        child.popen_kwargs = kwargs
        child.args = list(argv)
        self.spawned.append(child)
        return child


# ---------------------------------------------------------------------------
# Fixtures and observation helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_active_proc():
    """Reset the shared active-child global around each test."""
    state.active_proc = None
    try:
        yield
    finally:
        state.active_proc = None


@pytest.fixture
def child_env(monkeypatch):
    """Patch the resolver, process factory, clock and kill primitives with offline fakes."""
    state.active_proc = None
    trace: list[str] = []
    sleeps: list = []
    registry: dict[int, _ScriptedChild] = {}
    spawn = _SpawnRecorder()
    pids = iter(range(4000, 4100))

    monkeypatch.setattr(codex_cli.shutil, "which", lambda name, *a, **k: FAKE_BIN if name == "codex" else None)
    monkeypatch.setattr(codex_cli.subprocess, "Popen", spawn)
    monkeypatch.setattr(codex_cli.time, "sleep", sleeps.append)
    monkeypatch.setattr(process_control.os, "getpgid", lambda pid: pid)

    def fake_killpg(pgid, sig):
        target = registry.get(pgid)
        if target is None:
            raise ProcessLookupError(pgid)
        target.killed = True
        trace.append("killpg")

    monkeypatch.setattr(process_control.os, "killpg", fake_killpg)

    def child(stdout_lines, returncode=0, stderr="", effect=None):
        scripted = _ScriptedChild(next(pids), stdout_lines, stderr, returncode, effect, trace)
        registry[scripted.pid] = scripted
        spawn.queue.append(scripted)
        return scripted

    return SimpleNamespace(trace=trace, spawn=spawn, sleeps=sleeps, child=child)


@dataclass
class _Observed:
    result: object
    exc: BaseException | None


def _invoke(backend, **run_kwargs) -> _Observed:
    """Call the real run and record its result or any exception, whatever it is."""
    try:
        return _Observed(backend.run(**run_kwargs), None)
    except BaseException as exc:  # noqa: BLE001 — SystemExit and KeyboardInterrupt are observed too
        return _Observed(None, exc)


def _reaped_after_kill(trace: list[str]) -> bool:
    """True only if a reap follows the first termination entry."""
    terminations = [i for i, entry in enumerate(trace) if entry in ("killpg", "kill")]
    if not terminations:
        return False
    return "reap" in trace[terminations[0] + 1:]


def _run_kwargs(tmp_path, **overrides) -> dict:
    kwargs = dict(
        role=AgentRole.IMPLEMENTER,
        prompt="assignment",
        cwd=str(tmp_path),
        instructions="role policy",
        session_id=None,
    )
    kwargs.update(overrides)
    return kwargs


def _argv_shapes(argvs):
    return [(a[0], "resume" in a, a[-1]) for a in argvs]


def _continuation_targets(argvs):
    return [a[-2] for a in argvs if "resume" in a]


# ---------------------------------------------------------------------------
# Real-pipe offline executable
# ---------------------------------------------------------------------------

_PIPE_CHILD_BODY = '''
import hashlib, json, signal, sys

signal.alarm(10)


def emit(stream, obj):
    stream.write(json.dumps(obj, separators=(",", ":")) + "\\n")


out, err = sys.stdout, sys.stderr
emit(out, {"type": "thread.started", "thread_id": CREATED})
filler = "r" * 56
for i in range(4096):
    emit(out, {"type": "item.completed", "item": {"type": "reasoning", "text": "%d %s" % (i, filler)}})
for i in range(4096):
    err.write("log line %d %s\\n" % (i, filler))
out.flush()
err.flush()
data = sys.stdin.buffer.read()
digest = hashlib.sha256(data).hexdigest()
emit(out, {"type": "item.completed", "item": {"type": "agent_message", "text": digest}})
emit(out, {"type": "turn.completed"})
out.flush()
'''

_PIPE_CHILD_SOURCE = "CREATED = %r\n" % CREATED_UUID + _PIPE_CHILD_BODY


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------


class TestCodexChildLifetime:
    def test_normal_completion_owns_pipes_and_reaps(self, child_env, tmp_path):
        """A completed turn owns the child while communicating, buffers all three pipes and reaps it."""
        child = child_env.child(
            [_thread_started(CREATED_UUID), _agent_message("final answer"), _turn_completed()],
            returncode=0,
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        spawn_kwargs = {
            key: child.popen_kwargs.get(key)
            for key in ("stdin", "stdout", "stderr", "start_new_session", "cwd")
        }
        assert observed.exc is None
        assert observed.result == ("final answer", CREATED_UUID)
        assert child.owned_during_communicate == [True]
        assert child.inputs == ["assignment"]
        assert spawn_kwargs == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
            "cwd": str(tmp_path),
        }
        assert child.reaped is True
        assert state.active_proc is None

    def test_communication_failure_terminates_reaps_and_preserves_exception(self, child_env, tmp_path):
        """A pipe failure kills the child through shared cleanup, reaps it and re-raises the original error."""
        root = ValueError("root cause")
        original = OSError("pipe failure")
        original.__cause__ = root
        child = child_env.child([], effect=original)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.exc is original
        assert getattr(observed.exc, "__cause__", None) is root
        assert child.owned_during_communicate[:1] == [True]
        assert child.killed is True
        assert "killpg" in child_env.trace
        assert _reaped_after_kill(child_env.trace)
        assert state.active_proc is None

    def test_keyboard_interrupt_terminates_reaps_and_preserves_exception(self, child_env, tmp_path):
        """An interrupt during communication kills the child through shared cleanup, reaps it and re-raises."""
        interrupt = KeyboardInterrupt()
        child = child_env.child([], effect=interrupt)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.exc is interrupt
        assert child.owned_during_communicate[:1] == [True]
        assert child.killed is True
        assert "killpg" in child_env.trace
        assert _reaped_after_kill(child_env.trace)
        assert state.active_proc is None

    def test_force_quit_clears_shared_ownership_yet_runner_reaps_local_child(self, child_env, tmp_path):
        """After force quit clears the shared reference, the runner still reaps its retained child."""
        exit_exc = SystemExit(1)

        def force_quit():
            process_control.kill_active_child()
            raise exit_exc

        child = child_env.child([], effect=force_quit)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.exc is exit_exc
        assert child.owned_during_communicate[:1] == [True]
        assert child.killed is True
        assert _reaped_after_kill(child_env.trace)
        assert state.active_proc is None
        assert child.reaped is True

    def test_retry_resumes_identity_created_by_failed_fresh_attempt(self, child_env, tmp_path):
        """A transient failure of a fresh attempt retries by resuming the thread that attempt created."""
        first = child_env.child(
            [_thread_started(CREATED_UUID), _turn_failed("request timed out")],
            returncode=1,
        )
        second = child_env.child(
            [_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()],
            returncode=0,
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=None))

        spawn = child_env.spawn
        assert observed.result == ("done", CREATED_UUID)
        assert _argv_shapes(spawn.argvs) == [(FAKE_BIN, False, "-"), (FAKE_BIN, True, "-")]
        assert _continuation_targets(spawn.argvs) == [CREATED_UUID]
        assert spawn.spawn_checks == [(True, True), (True, True)]
        assert [first.owned_during_communicate, second.owned_during_communicate] == [[True], [True]]
        assert child_env.sleeps == [RETRY_DELAY_SECONDS]
        assert state.active_proc is None

    def test_retry_keeps_caller_supplied_identity_when_failed_attempt_emits_none(self, child_env, tmp_path):
        """A caller-supplied identity stays the continuation target even if the failed attempt emits none."""
        child_env.child([_turn_failed("request timed out")], returncode=1)
        child_env.child(
            [_thread_started(SUPPLIED_UUID), _agent_message("done"), _turn_completed()],
            returncode=0,
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=SUPPLIED_UUID))

        spawn = child_env.spawn
        assert observed.result == ("done", SUPPLIED_UUID)
        assert _argv_shapes(spawn.argvs) == [(FAKE_BIN, True, "-"), (FAKE_BIN, True, "-")]
        assert _continuation_targets(spawn.argvs) == [SUPPLIED_UUID, SUPPLIED_UUID]
        assert spawn.spawn_checks == [(True, True), (True, True)]
        assert child_env.sleeps == [RETRY_DELAY_SECONDS]
        assert state.active_proc is None

    def test_pipe_child_returns_digest_of_large_assignment_without_deadlock(self, tmp_path, monkeypatch):
        """Pipe-sized output on both streams and a megabyte assignment complete without deadlock."""
        executable = tmp_path / "codex"
        executable.write_text(f"#!{sys.executable}\n{_PIPE_CHILD_SOURCE}")
        os.chmod(executable, 0o755)
        sleeps: list = []
        monkeypatch.setattr(
            codex_cli.shutil, "which", lambda name, *a, **k: str(executable) if name == "codex" else None
        )
        monkeypatch.setattr(codex_cli.time, "sleep", sleeps.append)
        prompt = "".join(f"line {i:06d} assignment payload\n" for i in range(40000))

        observed = _invoke(
            CodexAgentBackend(),
            role=AgentRole.IMPLEMENTER,
            prompt=prompt,
            cwd=str(tmp_path),
            instructions="role policy",
        )

        assert observed.result == (hashlib.sha256(prompt.encode()).hexdigest(), CREATED_UUID)
        assert state.active_proc is None


# ---------------------------------------------------------------------------
# Invocation policy
# ---------------------------------------------------------------------------

_ROLE_MODELS = [
    (AgentRole.PLANNER_REVIEWER, "gpt-6.1-sol"),
    (AgentRole.PLAN_REVIEWER, "gpt-6.1-sol"),
    (AgentRole.IMPLEMENTER, "gpt-6.1-sol"),
]


def _ok_child(child_env, thread=CREATED_UUID):
    return child_env.child([_thread_started(thread), _agent_message("done"), _turn_completed()])


class TestCodexInvocation:
    @pytest.mark.parametrize("args", [(), (None,), ({},), ({"implementer": {"model": "custom"}},)])
    def test_construction_without_executable_raises_and_spawns_nothing(self, child_env, monkeypatch, args):
        """A missing executable is an invocation fault at construction, before any process exists."""
        monkeypatch.setattr(codex_cli.shutil, "which", lambda name, *a, **k: None)

        with pytest.raises(FileNotFoundError):
            CodexAgentBackend(*args)

        assert child_env.spawn.argvs == []

    @pytest.mark.parametrize("args", [(), (None,), ({},), ({"implementer": {"model": "custom"}},)])
    def test_construction_with_executable_spawns_nothing(self, child_env, args):
        """Resolving the executable never launches a process."""
        CodexAgentBackend(*args)

        assert child_env.spawn.argvs == []

    @pytest.mark.parametrize("args", [(), (None,), ({},)])
    def test_absent_settings_expose_complete_defaults_in_role_order(self, child_env, args):
        backend = CodexAgentBackend(*args)

        assert backend.role_settings == {
            role: RoleSetting(model, "high") for role, model in _ROLE_MODELS
        }
        assert list(backend.role_settings) == list(AgentRole)

    @pytest.mark.parametrize("role", list(AgentRole))
    @pytest.mark.parametrize("override", [
        {"model": "custom-model"},
        {"effort": "custom-effort"},
        {"model": "custom-model", "effort": "custom-effort"},
    ])
    def test_role_overrides_reach_fresh_and_continuing_turns(self, child_env, tmp_path, role, override):
        backend = CodexAgentBackend({role.value: override})
        expected = {key: RoleSetting(model, "high") for key, model in _ROLE_MODELS}
        expected[role] = RoleSetting(
            override.get("model", expected[role].model),
            override.get("effort", expected[role].effort),
        )
        assert backend.role_settings == expected
        assert list(backend.role_settings) == list(AgentRole)

        for session_id in (None, SUPPLIED_UUID):
            thread = session_id or CREATED_UUID
            child = _ok_child(child_env, thread)
            observed = _invoke(backend, **_run_kwargs(tmp_path, role=role, session_id=session_id))

            assert observed.exc is None
            assert observed.result == ("done", thread)
            argv = child.args
            assert argv[argv.index("-m") + 1] == expected[role].model
            assert [a for a in argv if a.startswith("model_reasoning_effort=")] == [
                f'model_reasoning_effort="{expected[role].effort}"'
            ]
            assert [a for a in argv if a.startswith("developer_instructions=")] == (
                [] if session_id else ['developer_instructions="role policy"']
            )
            assert ("resume" in argv) is (session_id is not None)
            if session_id:
                assert argv[-2] == session_id
            assert child.inputs == ["assignment"]

    def test_settings_mapping_and_values_are_immutable(self, child_env):
        backend = CodexAgentBackend()

        with pytest.raises(TypeError):
            backend.role_settings[AgentRole.IMPLEMENTER] = RoleSetting("other", "low")
        with pytest.raises(AttributeError):
            backend.role_settings[AgentRole.IMPLEMENTER].model = "other"
        with pytest.raises(AttributeError):
            backend.role_settings = {}

    def test_caller_mutation_does_not_change_snapshot_or_default_backend(self, child_env, tmp_path):
        agents = {"implementer": {"model": "custom-model", "effort": "custom-effort"}}
        backend = CodexAgentBackend(agents)
        agents["implementer"]["model"] = "later-model"
        agents["implementer"]["effort"] = "later-effort"
        agents["plan_reviewer"] = {"model": "later-review-model"}
        default_backend = CodexAgentBackend()
        defaults = {role: RoleSetting(model, "high") for role, model in _ROLE_MODELS}
        expected = {**defaults, AgentRole.IMPLEMENTER: RoleSetting("custom-model", "custom-effort")}

        assert backend.role_settings == expected
        assert default_backend.role_settings == defaults
        for current, settings in ((backend, expected), (default_backend, defaults)):
            for session_id in (None, SUPPLIED_UUID):
                thread = session_id or CREATED_UUID
                child = _ok_child(child_env, thread)
                observed = _invoke(current, **_run_kwargs(tmp_path, session_id=session_id))

                assert observed.exc is None
                assert observed.result == ("done", thread)
                argv = child.args
                assert argv[argv.index("-m") + 1] == settings[AgentRole.IMPLEMENTER].model
                element = next(a for a in argv if a.startswith("model_reasoning_effort="))
                assert tomllib.loads(element) == {"model_reasoning_effort": settings[AgentRole.IMPLEMENTER].effort}

    @pytest.mark.parametrize("session_id", [None, SUPPLIED_UUID])
    @pytest.mark.parametrize("effort", [
        'quote " and backslash \\',
        'high"\napproval_policy="always',
        'line\nbreak\ttab \U0001F600 del\x7f ctl\x01 end',
    ])
    def test_effort_round_trips_as_one_toml_value(self, child_env, tmp_path, session_id, effort):
        backend = CodexAgentBackend({"implementer": {"effort": effort}})
        thread = session_id or CREATED_UUID
        child = _ok_child(child_env, thread)

        observed = _invoke(backend, **_run_kwargs(tmp_path, session_id=session_id))

        assert observed.exc is None
        assert observed.result == ("done", thread)
        elements = [a for a in child.args if a.startswith("model_reasoning_effort=")]
        assert len(elements) == 1
        assert tomllib.loads(elements[0]) == {"model_reasoning_effort": effort}
        argv = child.args
        assert argv[argv.index(elements[0]) - 1] == "-c"
        assert len(argv) == (16 if session_id else 18)
        assert argv.count("-c") == (2 if session_id else 3)
        assert argv[argv.index('approval_policy="never"') - 1] == "-c"
        assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
        assert argv[argv.index("-s") + 1] == "danger-full-access"
        assert argv[argv.index("-C") + 1] == str(tmp_path)
        assert ("resume" in argv) is (session_id is not None)
        assert argv[-1] == "-"
        assert child.inputs == ["assignment"]

    def test_retry_keeps_resolved_settings_when_continuing_created_thread(self, child_env, tmp_path):
        backend = CodexAgentBackend({"implementer": {"model": "custom-model", "effort": "low"}})
        first = child_env.child(
            [_thread_started(CREATED_UUID), _turn_failed("request timed out")], returncode=1,
        )
        second = _ok_child(child_env)

        observed = _invoke(backend, **_run_kwargs(tmp_path))

        assert observed.exc is None
        assert observed.result == ("done", CREATED_UUID)
        assert _argv_shapes(child_env.spawn.argvs) == [(FAKE_BIN, False, "-"), (FAKE_BIN, True, "-")]
        assert _continuation_targets(child_env.spawn.argvs) == [CREATED_UUID]
        for child in (first, second):
            assert child.args[child.args.index("-m") + 1] == "custom-model"
            assert 'model_reasoning_effort="low"' in child.args
            assert child.inputs == ["assignment"]

    @pytest.mark.parametrize("role,model", _ROLE_MODELS)
    def test_fresh_form_matches_template_and_keeps_instructions_off_stdin(self, child_env, tmp_path, role, model):
        """A fresh turn passes the instructions as the developer element and only the assignment on stdin."""
        child = _ok_child(child_env)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, role=role, instructions="role policy"))

        assert observed.exc is None
        assert observed.result == ("done", CREATED_UUID)
        assert child_env.spawn.argvs == [[
            FAKE_BIN, "exec", "--json", "--color", "never",
            "-m", model,
            "-c", 'model_reasoning_effort="high"',
            "-c", 'approval_policy="never"',
            "-s", "danger-full-access",
            "-C", str(tmp_path),
            "-c", 'developer_instructions="role policy"',
            "-",
        ]]
        assert child.inputs == ["assignment"]

    def test_continuation_form_matches_template_and_omits_developer_instructions(self, child_env, tmp_path):
        """A continuation carries no developer instructions and still delivers only the assignment."""
        child = _ok_child(child_env, SUPPLIED_UUID)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=SUPPLIED_UUID))

        assert observed.exc is None
        assert observed.result == ("done", SUPPLIED_UUID)
        argv = child_env.spawn.argvs[0]
        assert argv == [
            FAKE_BIN, "exec",
            "-C", str(tmp_path),
            "-s", "danger-full-access",
            "-c", 'approval_policy="never"',
            "resume", "--json",
            "-m", "gpt-6.1-sol",
            "-c", 'model_reasoning_effort="high"',
            SUPPLIED_UUID,
            "-",
        ]
        assert not any(a.startswith("developer_instructions=") for a in argv)
        assert child.inputs == ["assignment"]

    @pytest.mark.parametrize("session_id", [None, SUPPLIED_UUID])
    def test_excluded_flags_never_appear(self, child_env, tmp_path, session_id):
        """Neither form uses the flags that would bypass user configuration or drop thread persistence."""
        _ok_child(child_env, session_id or CREATED_UUID)

        _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=session_id))

        argv = child_env.spawn.argvs[0]
        for banned in ("--ignore-user-config", "--ignore-rules", "--last", "--ephemeral"):
            assert banned not in argv
        assert not any("project_doc_max_bytes" in a for a in argv)

    def test_child_environment_drops_caller_identity_and_keeps_the_rest(self, child_env, tmp_path, monkeypatch):
        """The child inherits authentication and home, but none of the enclosing session's identity."""
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID"):
            monkeypatch.setenv(name, "caller")
        monkeypatch.setenv("OPENAI_API_KEY", "key-value")
        monkeypatch.setenv("CODEX_HOME", "/codex/home")
        child = _ok_child(child_env)

        _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        env = child.popen_kwargs["env"]
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID"):
            assert name not in env
        assert env["OPENAI_API_KEY"] == "key-value"
        assert env["CODEX_HOME"] == "/codex/home"
        assert child.popen_kwargs["cwd"] == str(tmp_path)

    def test_toml_encoding_round_trips_special_characters(self, child_env, tmp_path):
        """The developer element decodes as TOML to the exact instructions, keeping Unicode literal."""
        original = 'quote " back \\ line\nbreak\ttab \U0001F600 del\x7f ctl\x01 end'
        _ok_child(child_env)

        _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, instructions=original))

        element = next(a for a in child_env.spawn.argvs[0] if a.startswith("developer_instructions="))
        value = element[len("developer_instructions="):]
        assert tomllib.loads("v = " + value)["v"] == original
        assert "\U0001F600" in value
        assert "\x7f" not in value
        assert "\\ud83d" not in value.lower()

    def test_toml_encoding_rejects_lone_surrogate(self):
        """A lone surrogate cannot be represented in TOML and is rejected."""
        with pytest.raises(ValueError):
            codex_cli._toml_basic_string("\ud800")


# ---------------------------------------------------------------------------
# Completion and stream defects
# ---------------------------------------------------------------------------


class TestCodexCompletion:
    def test_returns_last_agent_message_and_ignores_other_events(self, child_env, tmp_path):
        """The last agent message is the result; tool, reasoning and unknown events add nothing."""
        child_env.child([
            _thread_started(CREATED_UUID),
            _agent_message("first"),
            _frame({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}}),
            _frame({"type": "item.completed", "item": {"type": "command_execution", "output": "tool out"}}),
            _frame({"type": "some.future.event", "payload": 1}),
            _agent_message("last"),
            _frame({"type": "item.completed", "item": {"type": "reasoning", "text": "after"}}),
            _turn_completed(),
        ])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("last", CREATED_UUID)

    def test_raw_unicode_line_separators_inside_a_message_survive(self, child_env, tmp_path):
        """Raw U+2028 and U+0085 inside a JSON string never split a record."""
        text = "before\u2028middle\u0085after"
        line = json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
            ensure_ascii=False,
        )
        assert "\u2028" in line and "\u0085" in line
        child_env.child([_thread_started(CREATED_UUID), line, _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == (text, CREATED_UUID)
        assert len(child_env.spawn.argvs) == 1

    def test_stale_errors_do_not_affect_a_successful_turn(self, child_env, tmp_path):
        """Earlier error events and stderr noise are ignored once the turn completes."""
        child_env.child(
            [
                _thread_started(CREATED_UUID),
                _error_event("You\u2019ve hit your usage limit"),
                _agent_message("done"),
                _turn_completed(),
            ],
            stderr="WARN something\nError: Quota exceeded. Check your plan and billing details.\n",
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("done", CREATED_UUID)
        assert len(child_env.spawn.argvs) == 1
        assert child_env.sleeps == []

    @pytest.mark.parametrize(
        "lines",
        [
            ["{not json", ],
            ["[1, 2]"],
            [_frame({"thread_id": CREATED_UUID})],
            [_frame({"type": 7})],
            [_frame({"type": "thread.started", "thread_id": 5})],
            [_frame({"type": "thread.started", "thread_id": "not-a-uuid"})],
            [_thread_started(CREATED_UUID), _thread_started(SUPPLIED_UUID)],
            [_thread_started(CREATED_UUID),
             _frame({"type": "item.completed", "item": {"type": "agent_message", "text": 5}}),
             _turn_completed()],
            [_thread_started(CREATED_UUID), _frame({"type": "turn.failed", "error": {}})],
            [_thread_started(CREATED_UUID), _agent_message("x"), _turn_completed(), _turn_failed("boom")],
        ],
        ids=[
            "malformed-json", "non-object", "missing-type", "non-string-type", "non-string-thread",
            "invalid-uuid", "two-identities", "non-string-text", "failed-without-message", "both-terminals",
        ],
    )
    def test_protocol_defects_raise_plain_runtime_error(self, child_env, tmp_path, lines):
        """A malformed consumed field is a defect, never a halt and never retried."""
        child_env.child(lines)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert str(observed.exc).splitlines()[0] == "Codex event stream is malformed"
        assert len(child_env.spawn.argvs) == 1
        assert child_env.sleeps == []

    def test_resume_emitting_a_different_thread_is_a_defect(self, child_env, tmp_path):
        """A continuation that reports another thread than the requested one is rejected."""
        child_env.child([_thread_started(CREATED_UUID), _agent_message("x"), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=SUPPLIED_UUID))

        assert type(observed.exc) is RuntimeError
        assert str(observed.exc).splitlines()[0] == "Codex resumed a different thread"
        assert child_env.sleeps == []

    @pytest.mark.parametrize(
        "lines,returncode",
        [
            ([_thread_started(CREATED_UUID), _agent_message("earlier"), _agent_message("  "), _turn_completed()], 0),
            ([_agent_message("done"), _turn_completed()], 0),
            ([_thread_started(CREATED_UUID), _agent_message("done")], 0),
            ([_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()], 1),
        ],
        ids=["empty-final-message", "no-identity", "no-terminal-event", "nonzero-exit"],
    )
    def test_incomplete_invocations_fail_without_retry(self, child_env, tmp_path, lines, returncode):
        """Completion evidence that is partial or contradicted by the exit status is not success."""
        child_env.child(lines, returncode=returncode)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert len(child_env.spawn.argvs) == 1
        assert child_env.sleeps == []

    def test_fresh_attempt_without_identity_retries_fresh(self, child_env, tmp_path):
        """A fresh attempt that created no thread retries as a fresh turn."""
        child_env.child([_turn_failed("request timed out")], returncode=1)
        child_env.child([_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("done", CREATED_UUID)
        assert _argv_shapes(child_env.spawn.argvs) == [(FAKE_BIN, False, "-"), (FAKE_BIN, False, "-")]

    def test_retry_bound_raises_network_error_after_three_attempts(self, child_env, tmp_path):
        """Three transient failures raise the halt after exactly three spawns and two delays."""
        for _ in range(3):
            child_env.child([_turn_failed("request timed out")], returncode=1)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert isinstance(observed.exc, NetworkError)
        assert len(child_env.spawn.argvs) == 3
        assert child_env.sleeps == [RETRY_DELAY_SECONDS, RETRY_DELAY_SECONDS]

    def test_retry_bound_resumes_the_created_thread(self, child_env, tmp_path):
        """After the first attempt creates a thread, later attempts resume it."""
        for _ in range(3):
            child_env.child([_thread_started(CREATED_UUID), _turn_failed("request timed out")], returncode=1)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert isinstance(observed.exc, NetworkError)
        assert _argv_shapes(child_env.spawn.argvs) == [
            (FAKE_BIN, False, "-"), (FAKE_BIN, True, "-"), (FAKE_BIN, True, "-"),
        ]
        assert _continuation_targets(child_env.spawn.argvs) == [CREATED_UUID, CREATED_UUID]

    def test_failed_turn_with_plausible_artifact_never_returns(self, child_env, tmp_path):
        """A failed turn raises even when the agent left a plausible artifact and said so."""
        artifact = tmp_path / "plan.md"
        child_env.child(
            [
                _thread_started(CREATED_UUID),
                _agent_message("I wrote plan.md"),
                _turn_failed("something unrecognized happened"),
            ],
            returncode=1,
            effect=lambda: artifact.write_text("# plan"),
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert artifact.exists()
        assert observed.result is None
        assert type(observed.exc) is RuntimeError
        assert str(observed.exc).splitlines()[0] == "Codex invocation failed"


# ---------------------------------------------------------------------------
# Supplied identity preflight
# ---------------------------------------------------------------------------

HEX_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

_INVALID_SUPPLIED = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace"),
    pytest.param("my-thread", id="thread-name"),
    pytest.param("--last", id="option-last"),
    pytest.param("-c", id="option-c"),
    pytest.param(" " + SUPPLIED_UUID, id="leading-space"),
    pytest.param(SUPPLIED_UUID + "\n", id="trailing-newline"),
    pytest.param(HEX_UUID.upper(), id="uppercase"),
    pytest.param(HEX_UUID.replace("-", ""), id="compact"),
    pytest.param("{" + HEX_UUID + "}", id="braced"),
    pytest.param("urn:uuid:" + HEX_UUID, id="urn"),
    pytest.param(123, id="int"),
    pytest.param(uuid.UUID(SUPPLIED_UUID), id="uuid-object"),
    pytest.param(SUPPLIED_UUID.encode(), id="bytes"),
]


@pytest.fixture
def builder_spies(monkeypatch):
    """Recording pass-through wrappers over both argument-vector builders."""
    calls = SimpleNamespace(fresh=[], resume=[])
    real_fresh, real_resume = codex_cli._fresh_argv, codex_cli._resume_argv

    def fresh(*args, **kwargs):
        calls.fresh.append(args)
        return real_fresh(*args, **kwargs)

    def resume(*args, **kwargs):
        calls.resume.append(args)
        return real_resume(*args, **kwargs)

    monkeypatch.setattr(codex_cli, "_fresh_argv", fresh)
    monkeypatch.setattr(codex_cli, "_resume_argv", resume)
    return calls


class TestCodexSuppliedIdentity:
    @pytest.mark.parametrize("supplied", _INVALID_SUPPLIED)
    def test_invalid_supplied_identity_dispatches_nothing(self, child_env, builder_spies, tmp_path, supplied):
        """A supplied value that is not canonical UUID text never reaches an argument builder or a process."""
        _ok_child(child_env)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=supplied))

        assert child_env.spawn.argvs == []
        assert builder_spies.fresh == []
        assert builder_spies.resume == []
        assert state.active_proc is None
        assert child_env.sleeps == []
        assert observed.result is None
        assert type(observed.exc) is ValueError
        assert str(observed.exc).splitlines()[0] == "Codex continuation identity is not a canonical UUID"

    def test_none_creates_a_fresh_thread(self, child_env, builder_spies, tmp_path):
        """An absent identity is the only path to creation: one fresh spawn, no continuation builder."""
        _ok_child(child_env)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=None))

        assert observed.exc is None
        assert len(child_env.spawn.argvs) == 1
        assert len(builder_spies.fresh) == 1
        assert builder_spies.resume == []
        assert observed.result == ("done", CREATED_UUID)

    def test_canonical_supplied_identity_continues_unchanged(self, child_env, builder_spies, tmp_path):
        """A canonical supplied identity reaches native continuation unchanged and is returned as the thread."""
        _ok_child(child_env, thread=SUPPLIED_UUID)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=SUPPLIED_UUID))

        assert observed.exc is None
        assert len(child_env.spawn.argvs) == 1
        assert [call[-1] for call in builder_spies.resume] == [SUPPLIED_UUID]
        assert builder_spies.fresh == []
        assert _continuation_targets(child_env.spawn.argvs) == [SUPPLIED_UUID]
        assert observed.result == ("done", SUPPLIED_UUID)

    def test_unknown_canonical_identity_neither_probed_nor_replaced(self, child_env, builder_spies, tmp_path):
        """A canonical but unknown identity reaches native continuation; its fault surfaces, no fresh thread is created."""
        child_env.child([_turn_failed("thread not found")], returncode=1)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path, session_id=HEX_UUID))

        assert len(child_env.spawn.argvs) == 1
        assert _continuation_targets(child_env.spawn.argvs) == [HEX_UUID]
        assert builder_spies.fresh == []
        assert child_env.sleeps == []
        assert type(observed.exc) is RuntimeError


# ---------------------------------------------------------------------------
# Failure recognition
# ---------------------------------------------------------------------------

_QUOTA_LIMIT_FORMS = [
    "You\u2019ve hit your usage limit",
    "You've hit your usage limit",
    "You\u2019ve hit your usage limit.",
    "You\u2019ve hit your usage limit for gpt-6-astra. Try again later.",
    "Your workspace is out of credits. Ask your admin to add credits.",
    "You hit your spend cap set in your workspace. Ask an admin to raise it.",
    "You hit your spend cap set by the owner of your workspace. Contact the owner.",
    "Quota exceeded. Check your plan and billing details.",
]

_TRANSIENT_FORMS = [
    "request timed out",
    "Selected model is at capacity. Please try a different model.",
] + [
    f"exceeded retry limit, last status: {status}{suffix}"
    for status in ("500 Internal Server Error", "502 Bad Gateway", "503 Service Unavailable", "504 Gateway Timeout")
    for suffix in ("", ", request id: req_123")
]

_UNKNOWN_FORMS = [
    "429 Too Many Requests",
    "Rate limit reached",
    "exceeded retry limit, last status: 429 Too Many Requests",
    "exceeded retry limit, last status: 500 Internal Server Error and more text",
    "500",
    "Connection failed: connection refused",
    "Error while reading the server response: eof",
    "stream disconnected before completion: eof",
    "stream disconnected before completion: reason: content_filter",
    "Your input exceeds the context window of this model.",
]


def _failed(child_env, message, stderr=""):
    child_env.child([_thread_started(CREATED_UUID), _turn_failed(message)], returncode=1, stderr=stderr)


class TestCodexFailureRecognition:
    @pytest.mark.parametrize("message", _QUOTA_LIMIT_FORMS)
    def test_quota_primary_raises_rate_limit_without_retry(self, child_env, tmp_path, message):
        """An exhausted-usage diagnostic halts immediately on a single spawn."""
        _failed(child_env, message)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert isinstance(observed.exc, RateLimitError)
        assert str(observed.exc).splitlines()[0] == "Codex reported exhausted usage"
        assert len(child_env.spawn.argvs) == 1
        assert child_env.sleeps == []

    def test_quota_from_stderr_error_record_only(self, child_env, tmp_path):
        """With no stream diagnostic, an `Error:`-prefixed stderr record supplies the quota primary."""
        child_env.child([], returncode=1, stderr="Error: You\u2019ve hit your usage limit\n")

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert isinstance(observed.exc, RateLimitError)
        assert len(child_env.spawn.argvs) == 1

    @pytest.mark.parametrize("message", _TRANSIENT_FORMS)
    def test_transient_primary_is_retried_to_success(self, child_env, tmp_path, message):
        """Each recognized transient form retries once and then succeeds."""
        _failed(child_env, message)
        child_env.child([_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("done", CREATED_UUID)
        assert len(child_env.spawn.argvs) == 2
        assert child_env.sleeps == [RETRY_DELAY_SECONDS]

    @pytest.mark.parametrize("message", _UNKNOWN_FORMS)
    def test_unrecognized_primary_raises_runtime_error_without_retry(self, child_env, tmp_path, message):
        """A diagnostic outside both positive lists surfaces as an invocation defect."""
        _failed(child_env, message)

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert str(observed.exc).splitlines()[0] == "Codex invocation failed"
        assert len(child_env.spawn.argvs) == 1
        assert child_env.sleeps == []

    def test_stderr_only_session_error_is_unknown(self, child_env, tmp_path):
        """A stderr-only missing-session error with empty stdout is an invocation defect."""
        child_env.child([], returncode=1, stderr="Error: thread not found (code -32600)\n")

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert len(child_env.spawn.argvs) == 1

    def test_unknown_primary_surfaces_despite_transient_stderr(self, child_env, tmp_path):
        """An unknown stream primary is never rescued by a transient stderr record."""
        _failed(child_env, "mystery failure", stderr="Error: request timed out\n")

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert child_env.sleeps == []

    def test_transient_primary_with_quota_stderr_record_is_rate_limit(self, child_env, tmp_path):
        """A quota stderr record upgrades a transient primary to exhausted usage."""
        _failed(child_env, "request timed out",
                stderr="Error: You\u2019ve hit your usage limit\n")

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert isinstance(observed.exc, RateLimitError)
        assert child_env.sleeps == []

    def test_transient_primary_with_unknown_stderr_record_is_defect(self, child_env, tmp_path):
        """An unrecognized stderr error record blocks an otherwise retryable primary."""
        _failed(child_env, "request timed out", stderr="Error: invalid credentials\n")

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert child_env.sleeps == []

    def test_transient_primary_with_plain_warnings_retries(self, child_env, tmp_path):
        """Stderr warnings that are not `Error:` records never block a retry."""
        _failed(child_env, "request timed out", stderr="WARN slow network\nINFO reconnecting\n")
        child_env.child([_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("done", CREATED_UUID)

    def test_historical_error_event_is_not_auxiliary(self, child_env, tmp_path):
        """With a terminal failure as primary, an earlier quota error event does not count."""
        child_env.child(
            [
                _thread_started(CREATED_UUID),
                _error_event("You\u2019ve hit your usage limit"),
                _turn_failed("request timed out"),
            ],
            returncode=1,
        )
        child_env.child([_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("done", CREATED_UUID)

    def test_last_error_event_is_primary_over_stderr(self, child_env, tmp_path):
        """Without a terminal failure, the last error event outranks stderr records."""
        child_env.child(
            [_thread_started(CREATED_UUID), _error_event("mystery failure")],
            returncode=1,
            stderr="Error: You\u2019ve hit your usage limit\n",
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert child_env.sleeps == []

    def test_quota_phrase_on_following_stderr_line_does_not_count(self, child_env, tmp_path):
        """Only the `Error:` line is a record; a quota phrase on the next line is not searched."""
        _failed(child_env, "request timed out",
                stderr="Error: request timed out\nYou\u2019ve hit your usage limit\n")
        child_env.child([_thread_started(CREATED_UUID), _agent_message("done"), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == ("done", CREATED_UUID)

    @pytest.mark.parametrize("content", ["request timed out", "You\u2019ve hit your usage limit"])
    def test_message_content_never_classifies_a_failed_turn(self, child_env, tmp_path, content):
        """Agent message text is content: an unknown native diagnostic still surfaces as a defect."""
        child_env.child(
            [_thread_started(CREATED_UUID), _agent_message(content), _turn_failed("mystery failure")],
            returncode=1,
        )

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert type(observed.exc) is RuntimeError
        assert child_env.sleeps == []

    @pytest.mark.parametrize("content", ["request timed out", "You\u2019ve hit your usage limit"])
    def test_successful_turn_returns_such_text(self, child_env, tmp_path, content):
        """A completed turn whose final message reads like a fault is still a success."""
        child_env.child([_thread_started(CREATED_UUID), _agent_message(content), _turn_completed()])

        observed = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path))

        assert observed.result == (content, CREATED_UUID)

    def test_halt_messages_open_with_a_fixed_line_and_carry_evidence_after(self, child_env, tmp_path):
        """The first line is fixed; exit status and native output follow on later lines."""
        _failed(child_env, "You\u2019ve hit your usage limit", stderr="native stderr text")
        quota = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path)).exc
        for _ in range(3):
            _failed(child_env, "request timed out")
        transient = _invoke(CodexAgentBackend(), **_run_kwargs(tmp_path)).exc

        assert isinstance(quota, RateLimitError)
        assert str(quota).splitlines()[0] == "Codex reported exhausted usage"
        assert "exit status: 1" in str(quota)
        assert "native stderr text" in str(quota)
        assert isinstance(transient, NetworkError)
        assert str(transient).splitlines()[0] == "Codex transient failure persisted through retries"
        assert "exit status: 1" in str(transient)


# ---------------------------------------------------------------------------
# Incremental account-quota dialogue and lifecycle witnesses
# ---------------------------------------------------------------------------

_QUOTA_REQUESTS = [
    {"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "orchestrator", "version": "0.1.0"}}},
    {"method": "initialized"},
    {"id": 2, "method": "account/rateLimits/read"},
]
_QUOTA_RESULT = {
    "rateLimitsByLimitId": {"codex": {"primary": {
        "usedPercent": 37, "windowDurationMins": 300}, "secondary": None}},
    "rateLimits": None,
    "witness": {"sequence": "initialized-account-open-stdin", "opaque": [3, "kept"]},
}
_REAL_MONOTONIC = time.monotonic


class _QuotaClock:
    """Elapsed time advances at lifecycle events, never at clock reads."""

    def __init__(self):
        self.elapsed = 0.0
        self.events = []

    def __call__(self):
        return self.elapsed

    def advance(self, event, seconds):
        self.elapsed += seconds
        self.events.append((event, self.elapsed))


class _QuotaPipe:
    """A bounded incremental pipe whose readable bytes follow client requests."""

    def __init__(self, child, name):
        self.child, self.name = child, name
        self.closed = False
        self.buffer = ""
        self.eof = False
        self.drained = False

    def _operation(self):
        child = self.child
        child.io_threads.add(threading.current_thread())
        if not child.first_io:
            child.first_io.append((self.name, state.active_proc is child))
        if self.closed:
            raise ValueError("I/O on a closed quota pipe")

    def write(self, data):
        with self.child.condition:
            self._operation()
            assert self.name == "stdin"
            self.child.inject()
            if self.child.ready_to_finish:
                raise BrokenPipeError("scripted quota child has exited")
            text = data.decode() if isinstance(data, bytes) else data
            self.buffer += text
            self.child.trace.append(("write", text))
            return len(data)

    def flush(self):
        with self.child.condition:
            self._operation()
            if self.name == "stdin":
                while "\n" in self.buffer:
                    line, self.buffer = self.buffer.split("\n", 1)
                    self.child.request(json.loads(line))
            self.child.condition.notify_all()

    def feed(self, text):
        self.buffer += text
        self.child.condition.notify_all()

    def _read(self, size, line):
        with self.child.condition:
            self._operation()
            self.child.pending_io += 1
            try:
                deadline = _REAL_MONOTONIC() + 0.5
                while not self.closed:
                    enough = "\n" in self.buffer if line else bool(self.buffer)
                    if enough or self.eof:
                        break
                    remaining = deadline - _REAL_MONOTONIC()
                    if remaining <= 0:
                        raise TimeoutError("scripted quota pipe made no dialogue progress")
                    self.child.condition.wait(remaining)
                if self.closed:
                    return "" if self.child.text_mode else b""
                count = len(self.buffer)
                if line and "\n" in self.buffer:
                    count = self.buffer.index("\n") + 1
                if size is not None and size >= 0:
                    count = min(count, size)
                data, self.buffer = self.buffer[:count], self.buffer[count:]
                if not data and self.eof:
                    self.drained = True
                if self.name == "stdout" and data:
                    self.child.delivered(data)
                self.child.trace.append(("read", self.name, len(data)))
                return data if self.child.text_mode else data.encode()
            finally:
                self.child.pending_io -= 1

    def readline(self, size=-1):
        return self._read(size, True)

    def read(self, size=-1):
        return self._read(size, False)

    read1 = read

    def fileno(self):
        return id(self)

    def __iter__(self):
        return self

    def __next__(self):
        data = self.readline()
        if not data:
            raise StopIteration
        return data

    def close(self):
        with self.child.condition:
            if self.closed:
                return
            self.closed = True
            self.child.trace.append(("close", self.name))
            if self.name == "stdin":
                self.child.stdin_closed_after_result = self.child.account_delivered
                self.child.ready_to_finish = True
                for pipe in (self.child.stdout, self.child.stderr):
                    pipe.eof = True
            self.child.condition.notify_all()


class _QuotaChild:
    """A child that only produces the next response after the right flushed request."""

    def __init__(self, pid, clock, mode="success", effect=None, stages=(0, 0, 0)):
        self.pid, self.clock, self.mode = pid, clock, mode
        self.effect, self.stages = effect, stages
        self.condition = threading.Condition(threading.RLock())
        self.stdin = _QuotaPipe(self, "stdin")
        self.stdout = _QuotaPipe(self, "stdout")
        self.stderr = _QuotaPipe(self, "stderr")
        self.trace, self.requests, self.first_io = [], [], []
        self.io_threads = set()
        self.pending_io = 0
        self.args, self.kwargs = [], {}
        self.text_mode = True
        self.returncode = None
        self.killed = self.reaped = self.ready_to_finish = False
        self.init_delivered = self.account_delivered = False
        self.stdin_open_at_result = self.stdin_closed_after_result = False
        self.shared_clear_at_injection = None
        self.finish_advanced = False
        self.waits = []
        self.injected = False
        self.delivery_buffer = ""

    def inject(self):
        if self.effect is None or self.injected:
            return
        self.injected = True
        self.trace.append(("inject",))
        if callable(self.effect):
            self.effect(self)
        else:
            raise self.effect

    def emit(self, obj):
        self.stdout.feed(_frame(obj) + "\n")

    def request(self, obj):
        position = len(self.requests)
        self.requests.append(obj)
        self.trace.append(("request", obj))
        if self.mode == "initialize-error" and position > 0:
            # Keep receiving forbidden follow-on frames so the client gate is observable.
            return
        assert position < 3 and obj == _QUOTA_REQUESTS[position], "unexpected quota request"
        if position > 0:
            assert self.init_delivered, "client advanced before the successful initialize response"
        if position == 0:
            self.stderr.feed("offline quota diagnostic\n")
            self.emit({"method": "notice", "params": {"stage": "initializing"}})
            self.emit({"id": 91, "result": {"unrelated": True}})
            if self.mode == "initialize-error":
                self.emit({"id": 1, "error": {"code": -1, "message": "no initialize"}})
            else:
                self.emit({"id": 1, "result": {"serverInfo": "offline"}})
            if self.mode == "initialize-only":
                self.exit_streams()
        elif position == 2:
            self.emit({"method": "notice", "params": {"stage": "account"}})
            self.emit({"id": 92, "result": {"unrelated": True}})
            if self.mode == "malformed-json":
                self.stdout.feed("{broken JSON\n")
            elif self.mode == "non-object":
                self.stdout.feed("[1, 2]\n")
            elif self.mode == "account-error":
                self.emit({"id": 2, "error": {"code": -2, "message": "no account"}})
            elif self.mode == "scalar-result":
                self.emit({"id": 2, "result": 17})
            elif self.mode != "wrong-id":
                self.emit({"id": 2, "result": _QUOTA_RESULT})
            if self.mode not in ("success", "nonzero-exit"):
                self.exit_streams()

    def delivered(self, text):
        # Only complete response frames release the corresponding next stage.
        self.delivery_buffer += text
        while "\n" in self.delivery_buffer:
            line, self.delivery_buffer = self.delivery_buffer.split("\n", 1)
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict) or "result" not in frame:
                continue
            if frame.get("id") == 1 and not self.init_delivered:
                self.init_delivered = True
                self.clock.advance("initialize", self.stages[0])
            if frame.get("id") == 2 and not self.account_delivered:
                self.account_delivered = True
                self.stdin_open_at_result = not self.stdin.closed
                self.clock.advance("account", self.stages[1])

    def exit_streams(self):
        self.ready_to_finish = True
        self.stdout.eof = self.stderr.eof = True
        self.condition.notify_all()

    def _finish(self, timeout=None):
        if not self.finish_advanced:
            self.finish_advanced = True
            duration = self.stages[2]
            if timeout is not None and timeout < duration:
                self.clock.advance("finish-timeout", max(0, timeout))
                raise subprocess.TimeoutExpired(self.args, timeout)
            self.clock.advance("finish", duration)
        self.returncode = 7 if self.mode == "nonzero-exit" else 0

    def poll(self):
        with self.condition:
            if self.killed:
                self.returncode = -9
            elif self.ready_to_finish and self.returncode is None:
                self._finish()
            return self.returncode

    def wait(self, timeout=None):
        with self.condition:
            self.waits.append((timeout, self.killed, self.clock(), self.returncode is not None))
            if self.killed:
                self.returncode = -9
            elif not self.ready_to_finish:
                raise subprocess.TimeoutExpired(self.args, timeout)
            elif self.returncode is None:
                self._finish(timeout)
            self.reaped = True
            self.trace.append(("reap",))
            return self.returncode

    def kill(self):
        with self.condition:
            self.killed = True
            self.trace.append(("kill",))
            self.exit_streams()

    terminate = kill

    def snapshot(self):
        return {
            "reaped": self.reaped,
            "closed": all(p.closed for p in (self.stdin, self.stdout, self.stderr)),
            "pending_io": self.pending_io,
            "workers_finished": all(t is threading.current_thread() or not t.is_alive()
                                    for t in self.io_threads),
            "ownership_clear": state.active_proc is None,
        }


@pytest.fixture
def quota_env(monkeypatch):
    """Offline seams plus state observed before teardown can repair any resource."""
    clock, queue, spawned, attempts, spawn_checks = _QuotaClock(), [], [], [], []
    registry = {}
    descriptors = {}
    real_read, real_set_blocking = os.read, os.set_blocking

    def set_blocking(descriptor, blocking):
        if descriptor not in descriptors:
            return real_set_blocking(descriptor, blocking)
        assert blocking is False

    def read(descriptor, size):
        if descriptor not in descriptors:
            return real_read(descriptor, size)
        pipe = descriptors[descriptor]
        with pipe.child.condition:
            if not pipe.buffer and not pipe.eof:
                pipe._operation()
                raise BlockingIOError()
            return pipe.read(size).encode()

    monkeypatch.setattr(codex_cli.os, "set_blocking", set_blocking)
    monkeypatch.setattr(codex_cli.os, "read", read)
    monkeypatch.setattr(codex_cli, "_resolve_codex", lambda: FAKE_BIN)
    monkeypatch.setattr(codex_cli.time, "monotonic", clock)
    for key, value in {"PATH": "/offline/path", "CODEX_HOME": "/offline/config",
                       "OPENAI_API_KEY": "offline-auth-placeholder",
                       "QUOTA_TEST": "preserved", "CODEX_THREAD_ID": "outer-thread",
                       "CODEX_SESSION_ID": "outer-session", "CODEX_TOOL_CALL_ID": "outer-tool"}.items():
        monkeypatch.setenv(key, value)

    def spawn(args, **kwargs):
        attempts.append((list(args), kwargs))
        spawn_checks.append([c.snapshot() for c in spawned])
        if not queue:
            raise AssertionError("unscripted quota child")
        child = queue.pop(0)
        if isinstance(child, BaseException):
            raise child
        child.args, child.kwargs = list(args), kwargs
        child.text_mode = bool(kwargs.get("text") or kwargs.get("encoding") or kwargs.get("universal_newlines"))
        spawned.append(child)
        return child

    monkeypatch.setattr(codex_cli.subprocess, "Popen", spawn)
    monkeypatch.setattr(process_control.os, "getpgid", lambda pid: pid)

    def killpg(pid, sig):
        assert sig == signal.SIGKILL
        registry[pid].trace.append(("killpg",))
        registry[pid].kill()

    monkeypatch.setattr(process_control.os, "killpg", killpg)

    def child(**kwargs):
        proc = _QuotaChild(6000 + len(registry), clock, **kwargs)
        registry[proc.pid] = proc
        for pipe in (proc.stdout, proc.stderr):
            descriptors[pipe.fileno()] = pipe
        queue.append(proc)
        return proc

    env = SimpleNamespace(clock=clock, queue=queue, child=child, spawned=spawned,
                          attempts=attempts, spawn_checks=spawn_checks)
    yield env
    for proc in spawned:
        proc.kill()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
        proc.wait()
        for worker in proc.io_threads:
            if worker is not threading.current_thread():
                worker.join(timeout=1)


def _observe_quota(tmp_path):
    """The helper's direct result/exception, before fixture cleanup runs."""
    try:
        return _Observed(codex_cli._read_rate_limits(str(tmp_path)), None)
    except BaseException as exc:
        return _Observed(None, exc)


def _assert_quota_clean(snapshot):
    assert snapshot == {"reaped": True, "closed": True, "pending_io": 0,
                        "workers_finished": True, "ownership_clear": True}


class TestCodexQuotaContract:
    def test_ordered_correlated_result_and_launch_context(self, quota_env, tmp_path):
        child = quota_env.child()

        observed = _observe_quota(tmp_path)
        snapshot = child.snapshot()

        assert observed.exc is None
        assert observed.result == _QUOTA_RESULT
        assert len(quota_env.attempts) == 1
        assert child.args == [FAKE_BIN, "app-server", "--stdio"]
        assert {k: child.kwargs[k] for k in ("cwd", "stdin", "stdout", "stderr", "start_new_session")} == {
            "cwd": str(tmp_path), "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "start_new_session": True,
        }
        environment = child.kwargs["env"]
        assert {k: environment[k] for k in ("PATH", "CODEX_HOME", "OPENAI_API_KEY", "QUOTA_TEST")} == {
            "PATH": "/offline/path", "CODEX_HOME": "/offline/config",
            "OPENAI_API_KEY": "offline-auth-placeholder", "QUOTA_TEST": "preserved",
        }
        assert all(k not in environment for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID"))
        assert child.first_io and child.first_io[0][1] is True
        assert child.requests == _QUOTA_REQUESTS
        assert child.stdin_open_at_result and child.stdin_closed_after_result
        assert child.stdout.drained and child.stderr.drained
        _assert_quota_clean(snapshot)

    @pytest.mark.parametrize("mode", [
        "initialize-only", "wrong-id", "scalar-result", "nonzero-exit",
        "malformed-json", "non-object", "initialize-error", "account-error",
    ])
    def test_unusable_dialogue_never_substitutes_process_success(self, quota_env, tmp_path, mode):
        child = quota_env.child(mode=mode)

        observed = _observe_quota(tmp_path)
        snapshot = child.snapshot()

        assert observed.exc is not None, "unusable quota dialogue returned as success"
        assert observed.result is None
        assert quota_env.spawned == [child]
        assert child.requests[:1] == _QUOTA_REQUESTS[:1]
        if mode == "initialize-error":
            assert child.requests == _QUOTA_REQUESTS[:1], "client advanced after initialization RPC error"
            assert child.stdin.buffer == "", "client buffered a forbidden follow-on frame"
        elif mode != "initialize-only":
            assert child.requests == _QUOTA_REQUESTS
        assert child.first_io and child.first_io[0][1] is True
        _assert_quota_clean(snapshot)

    def test_failed_launch_preserves_original_error_without_invented_child(self, quota_env, tmp_path):
        original = OSError("offline launch denied")
        quota_env.queue.append(original)

        observed = _observe_quota(tmp_path)

        assert observed.exc is original
        assert len(quota_env.attempts) == 1
        assert quota_env.attempts[0][0] == [FAKE_BIN, "app-server", "--stdio"]
        assert quota_env.spawned == []
        assert state.active_proc is None

    @pytest.mark.parametrize("fault", ["oserror", "keyboard", "force-quit"])
    def test_communication_fault_preserves_identity_and_local_cleanup(self, quota_env, tmp_path, fault):
        cause = ValueError("retained cause")
        original = {"oserror": OSError("offline broken pipe"),
                    "keyboard": KeyboardInterrupt(), "force-quit": SystemExit(3)}[fault]
        if fault == "oserror":
            original.__cause__ = cause

        def inject(child):
            if fault == "force-quit":
                process_control.kill_active_child()
                child.shared_clear_at_injection = state.active_proc is None
            raise original

        child = quota_env.child(effect=inject)

        observed = _observe_quota(tmp_path)
        snapshot = child.snapshot()

        assert observed.exc is original
        if fault == "oserror":
            assert observed.exc.__cause__ is cause
        assert quota_env.spawned == [child]
        assert child.injected and child.first_io[0][1] is True
        assert child.killed
        if fault == "force-quit":
            assert child.shared_clear_at_injection is True
        assert child.trace.index(("reap",)) > child.trace.index(("killpg",))
        _assert_quota_clean(snapshot)

    def test_later_query_observes_complete_previous_cleanup(self, quota_env, tmp_path):
        first = quota_env.child(mode="malformed-json")
        second = quota_env.child()

        failed = _observe_quota(tmp_path)
        first_snapshot = first.snapshot()
        succeeded = _observe_quota(tmp_path)
        second_snapshot = second.snapshot()

        assert failed.exc is not None
        assert succeeded.exc is None
        assert succeeded.result == _QUOTA_RESULT
        assert quota_env.spawned == [first, second]
        assert first.requests == second.requests == _QUOTA_REQUESTS
        _assert_quota_clean(first_snapshot)
        assert quota_env.spawn_checks[1] == [first_snapshot]
        _assert_quota_clean(second_snapshot)

    @pytest.mark.parametrize("stages,overrun", [
        ((17, 15, 0), "account"), ((12, 12, 8), "finish"),
    ], ids=["account-consumes-remaining-budget", "finish-consumes-remaining-budget"])
    def test_one_deadline_covers_dialogue_and_process_finish(self, quota_env, tmp_path, stages, overrun):
        child = quota_env.child(stages=stages)

        observed = _observe_quota(tmp_path)
        snapshot = child.snapshot()

        assert observed.exc is not None, "operation overran one total deadline and returned success"
        assert observed.result is None
        assert quota_env.spawned == [child]
        assert child.requests == _QUOTA_REQUESTS
        assert child.init_delivered and child.account_delivered
        assert quota_env.clock.elapsed >= 30
        if overrun == "finish":
            operation_waits = [timeout for timeout, killed, _, finished in child.waits
                               if not killed and not finished]
            assert any(event.startswith("finish") for event, _ in quota_env.clock.events)
            assert all(timeout is not None and 0 <= timeout <= 6 for timeout in operation_waits)
        assert child.killed or child.returncode is not None
        _assert_quota_clean(snapshot)

    def test_real_pipes_require_drainage_and_open_stdin(self, quota_executable, tmp_path):
        receipt = _supervise_quota_driver(quota_executable, tmp_path)

        assert receipt["exception"] is None, receipt
        assert receipt["result"] == _QUOTA_RESULT, receipt
        assert receipt["driver_timed_out"] is False
        assert receipt["spawned"] == 1
        assert receipt["returncodes"] == [0]
        assert receipt["reaped_before_return"] == [True]
        assert receipt["pipes_closed_before_return"] == [True]
        assert receipt["workers_finished_before_return"] is True
        assert receipt["ownership_clear_before_return"] is True
        assert receipt["cleanup_reaped"] == [True]

    @pytest.mark.parametrize("quota_executable", [True], indirect=True)
    @pytest.mark.parametrize("fault", ["deadline", "keyboard", "force-quit"])
    def test_exited_child_with_inherited_outputs_has_cancellable_readers(
        self, quota_executable, tmp_path, fault,
    ):
        receipt = _supervise_quota_driver(quota_executable, tmp_path, after_exit=fault)

        assert receipt["driver_timed_out"] is False, receipt
        assert receipt["direct_child_exited_before_fault"] is True, receipt
        assert receipt["inherited_writer_alive_before_cleanup"] is True, receipt
        assert receipt["cleanup_elapsed"] < 0.8, receipt
        assert receipt["exception_type"] == {
            "deadline": "TimeoutError", "keyboard": "KeyboardInterrupt",
            "force-quit": "SystemExit",
        }[fault], receipt
        if fault != "deadline":
            assert receipt["exception_identity_preserved"] is True, receipt
        assert receipt["returncodes"] == [0], receipt
        assert receipt["reaped_before_return"] == [True], receipt
        assert receipt["pipes_closed_before_return"] == [True], receipt
        assert receipt["workers_finished_before_return"] is True, receipt
        assert receipt["ownership_clear_before_return"] is True, receipt
        assert receipt["cleanup_reaped"] == [True], receipt


# ---------------------------------------------------------------------------
# A quota executable and isolated supervisor, all offline
# ---------------------------------------------------------------------------

_QUOTA_PIPE_BODY = r'''
import json, os, select, signal, sys, time

signal.alarm(4)
pending = b""


def request():
    global pending
    while b"\n" not in pending:
        readable, _, _ = select.select([0], [], [], 2)
        if not readable:
            raise RuntimeError("client request deadline")
        data = os.read(0, 65536)
        if not data:
            raise RuntimeError("early client EOF")
        pending += data
    line, pending = pending.split(b"\n", 1)
    return json.loads(line)


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


assert sys.argv[1:] == ["app-server", "--stdio"]
assert request() == EXPECTED[0]
# Each stream exceeds ordinary pipe capacity before the initialize reply.
for i in range(8192):
    emit({"method": "notice", "params": {"padding": "n" * 256, "index": i}})
for i in range(8192):
    sys.stderr.write("quota diagnostic %d %s\n" % (i, "e" * 256))
sys.stderr.flush()
emit({"method": "notice", "params": {"ready": True}})
emit({"id": 101, "result": {"unrelated": True}})
emit({"id": 1, "result": {"serverInfo": "offline"}})
assert request() == EXPECTED[1]
assert request() == EXPECTED[2]
assert not pending, "unexpected extra outbound frame"
# No request is expected now: readiness means either extra bytes or early EOF.
readable, _, _ = select.select([0], [], [], 0.05)
if readable:
    data = os.read(0, 65536)
    assert data, "stdin closed before correlated account reply"
    raise RuntimeError("unexpected extra client bytes")
emit({"id": 102, "result": {"unrelated": True}})
# This complete marker is emitted only after identity, ordering and open-input checks.
emit({"id": 2, "result": RESULT})
readable, _, _ = select.select([0], [], [], 2)
assert readable and os.read(0, 65536) == b"", "client did not close stdin after account reply"
if HOLD_OUTPUT:
    writer = os.fork()
    if writer == 0:
        time.sleep(1.5)
        os._exit(0)
    with open(WRITER_FILE, "w") as marker:
        marker.write(str(writer))
    os._exit(0)
'''


@pytest.fixture
def quota_executable(tmp_path, request):
    executable = tmp_path / "offline-quota"
    executable.write_text(
        f"#!{sys.executable}\nEXPECTED = {_QUOTA_REQUESTS!r}\nRESULT = {_QUOTA_RESULT!r}\n"
        f"HOLD_OUTPUT = {getattr(request, 'param', False)!r}\n"
        f"WRITER_FILE = {str(tmp_path / 'quota-inherited-writer.pid')!r}\n"
        + _QUOTA_PIPE_BODY
    )
    executable.chmod(0o755)
    return executable


def _quota_driver(connection, executable, cwd, pid_file, after_exit=None):
    """An isolated owner can always kill and reap its own separately sessioned child."""
    real_popen = subprocess.Popen
    children, reaped = [], set()
    baseline_threads = set(threading.enumerate())
    query_started = _REAL_MONOTONIC()
    deadline_trigger = None
    exited_before_fault = False
    late_exception = KeyboardInterrupt() if after_exit == "keyboard" else SystemExit(3)

    def record_lifecycle():
        destination = Path(pid_file)
        pending = destination.with_name(f"{destination.name}.{threading.get_ident()}.pending")
        pending.write_text(json.dumps({
            "pids": [child.pid for child in children], "reaped": sorted(reaped),
        }))
        os.replace(pending, destination)

    def cleanup():
        outcomes = []
        for proc in children:
            if proc.poll() is None or after_exit is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=1)
                outcomes.append(True)
            except BaseException:
                outcomes.append(False)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        return outcomes

    def interrupted(signum, frame):
        cleanup()
        raise SystemExit("quota driver stopped by supervisor")

    signal.signal(signal.SIGTERM, interrupted)

    def spawn(*args, **kwargs):
        # Register before unmasking supervision, so even interruption owns a handle.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            record_lifecycle()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        original_wait, original_poll = proc.wait, proc.poll

        def wait(*args, **kwargs):
            result = original_wait(*args, **kwargs)
            reaped.add(proc.pid)
            record_lifecycle()
            return result

        def poll(*args, **kwargs):
            result = original_poll(*args, **kwargs)
            if result is not None:
                reaped.add(proc.pid)
                record_lifecycle()
            return result

        proc.wait, proc.poll = wait, poll
        if after_exit is not None:
            original_close = proc.stdin.close
            triggered = False

            def close():
                nonlocal triggered, deadline_trigger, exited_before_fault
                original_close()
                if triggered:
                    return
                triggered = True
                # The direct child is reaped, but its descendant retains both
                # output writers. Cleanup cannot depend on that writer's EOF.
                proc.wait(timeout=1)
                exited_before_fault = proc.returncode == 0
                deadline_trigger = _REAL_MONOTONIC()
                if after_exit != "deadline":
                    if after_exit == "force-quit":
                        process_control.kill_active_child()
                    raise late_exception

            proc.stdin.close = close
        return proc

    codex_cli._resolve_codex = lambda: executable
    codex_cli.subprocess.Popen = spawn
    if after_exit == "deadline":
        def quota_clock():
            if deadline_trigger is None:
                return _REAL_MONOTONIC()
            return query_started + 29.8 + (_REAL_MONOTONIC() - deadline_trigger)

        codex_cli.time.monotonic = quota_clock
    try:
        try:
            observed = _Observed(codex_cli._read_rate_limits(cwd), None)
        except BaseException as exc:
            observed = _Observed(None, exc)
        # Capture before cleanup: the driver cannot repair a passing helper observation.
        receipt = {
            "result": observed.result,
            "exception": None if observed.exc is None else repr(observed.exc),
            "exception_type": None if observed.exc is None else type(observed.exc).__name__,
            "exception_identity_preserved": observed.exc is late_exception,
            "elapsed": _REAL_MONOTONIC() - query_started,
            "cleanup_elapsed": None if deadline_trigger is None else _REAL_MONOTONIC() - deadline_trigger,
            "direct_child_exited_before_fault": exited_before_fault,
            "inherited_writer_alive_before_cleanup": False,
            "spawned": len(children),
            "pids": [p.pid for p in children],
            "returncodes": [p.returncode for p in children],
            "reaped_before_return": [p.pid in reaped for p in children],
            "pipes_closed_before_return": [all(
                pipe is not None and pipe.closed for pipe in (p.stdin, p.stdout, p.stderr)
            ) for p in children],
            "ownership_clear_before_return": state.active_proc is None,
            "workers_finished_before_return": all(
                thread in baseline_threads or not thread.is_alive()
                for thread in threading.enumerate()
            ),
        }
        writer_file = Path(cwd) / "quota-inherited-writer.pid"
        if writer_file.exists():
            try:
                os.kill(int(writer_file.read_text()), 0)
                receipt["inherited_writer_alive_before_cleanup"] = True
            except ProcessLookupError:
                pass
        receipt["cleanup_reaped"] = cleanup()
        connection.send(receipt)
    finally:
        cleanup()
        connection.close()
    # A broken helper's surviving worker cannot keep this isolated owner alive.
    os._exit(0)


def _supervise_quota_driver(executable, tmp_path, *, after_exit=None):
    """Independent outer deadline; stop the owner while it can still reap its child."""
    context = multiprocessing.get_context("fork")
    reader, writer = context.Pipe(duplex=False)
    pid_file = tmp_path / "quota-child-pids.json"
    driver = context.Process(target=_quota_driver,
                             args=(writer, str(executable), str(tmp_path), str(pid_file), after_exit))
    driver.start()
    writer.close()
    timed_out = False
    receipt = None
    try:
        driver.join(timeout=7)
        timed_out = driver.is_alive()
        if timed_out:
            driver.terminate()
            driver.join(timeout=2)
        if reader.poll(0.1):
            try:
                receipt = reader.recv()
            except EOFError:
                pass
    finally:
        # The driver owns reaping; a fallback group kill covers a crashed owner too.
        lifecycle = json.loads(pid_file.read_text()) if pid_file.exists() else {"pids": [], "reaped": []}
        for pid in set(lifecycle["pids"]) - set(lifecycle["reaped"]):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        writer_file = tmp_path / "quota-inherited-writer.pid"
        if writer_file.exists():
            try:
                os.kill(int(writer_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if driver.is_alive():
            driver.kill()
        driver.join(timeout=2)
        reader.close()
        driver.close()
    if receipt is None:
        receipt = {"result": None, "exception": "quota driver produced no result"}
    receipt["driver_timed_out"] = timed_out
    return receipt

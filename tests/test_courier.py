"""The orchestrator's one peer send, judged with stand-ins: no real codex or claude."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import courier as courier_module
from orchestrator.courier import SendResult, send, send_codex

OWNER = "max"
FOLDER = "head"
CLAUDE = "/stand/bin/claude"
NAME = "the head"
TEXT = "Line one.\nLine two, with 'quotes' and \"doubles\"."


def _identity(tmp_path, owner=OWNER, folder=FOLDER):
    from orchestrator.locator import HeadIdentity
    return HeadIdentity(repository=tmp_path, owner=owner, folder=folder)


def _address(tmp_path, payload, lines=None):
    folder = tmp_path / ".ai-factory" / "architects" / OWNER / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (folder / "address.json").write_text(json.dumps(payload), encoding="utf-8")
    if lines is not None:
        (folder / "address.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


class Recorder:
    """A stand-in subprocess whose run records the call and answers scripted results."""

    def __init__(self, result=None, stdout="", returncode=0):
        self.calls = []
        self.result = result
        self.stdout = stdout
        self.returncode = returncode

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.result is not None:
            raise self.result
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout)


def _install(monkeypatch, recorder):
    monkeypatch.setattr(courier_module, "subprocess", SimpleNamespace(
        run=recorder.run,
        DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    ))
    monkeypatch.setattr(courier_module, "_resolve_claude", lambda: Path(CLAUDE))


# ---------------------------------------------------------------------------
# The Codex send


def test_the_dispatch_follows_the_address_s_runtime_and_nothing_else(tmp_path, monkeypatch):
    recorder = Recorder()
    _install(monkeypatch, recorder)
    _address(tmp_path, {"session-id": "tid", "runtime": "codex",
                        "session-name": "a name nobody uses", "permission-mode": "plan"})

    result = send(_identity(tmp_path), TEXT)

    assert result == SendResult(sent=True, reason=None)
    (argv, _) = recorder.calls[0]
    assert argv[0] == "codex"  # a Codex address, whatever the other fields say

    _address(tmp_path, {"session-id": "tid", "runtime": "claude", "session-name": NAME})
    result = send(_identity(tmp_path), TEXT)
    assert recorder.calls[1][0][0] == str(Path(CLAUDE))  # a Claude address, by runtime alone


def test_the_codex_argv_and_arguments_equal_the_launcher_s(tmp_path, monkeypatch):
    recorder = Recorder()
    _install(monkeypatch, recorder)

    result = send_codex("thread-id", TEXT)

    assert result == SendResult(sent=True, reason=None)
    (argv, kwargs) = recorder.calls[0]
    assert argv == ["codex", "queue", "--thread", "thread-id", "--message", TEXT]
    assert kwargs == dict(timeout=30, check=True, stdin=subprocess.DEVNULL,
                          stdout=sys.stdout, stderr=subprocess.STDOUT)


@pytest.mark.parametrize("failure,carried", [
    (subprocess.CalledProcessError(1, ["codex"]), "returned non-zero exit status 1"),
    (OSError("codex: command not found"), "codex: command not found"),
    (ValueError("embedded null byte"), "embedded null byte"),
])
def test_a_failed_codex_call_keeps_the_exception_s_text(monkeypatch, failure, carried):
    _install(monkeypatch, Recorder(result=failure))

    result = send_codex("thread-id", TEXT)

    assert result.sent is False
    assert carried in result.reason


def test_a_codex_timeout_names_the_bound_alone(monkeypatch):
    _install(monkeypatch, Recorder(result=subprocess.TimeoutExpired(
        cmd=["codex", "queue", "--thread", "thread-id", "--message", TEXT], timeout=30,
    )))

    result = send_codex("thread-id", TEXT)

    assert result.sent is False
    assert result.reason == "codex send timed out after 30 seconds"
    assert "thread-id" not in result.reason and TEXT not in result.reason


def test_a_send_without_a_standin_fails_instead_of_calling_the_executable():
    with pytest.raises(AssertionError):  # the conftest stand-in refuses the call
        send_codex("thread-id", TEXT)


def test_a_locator_fault_is_a_non_send_carrying_the_fault_s_text(tmp_path, monkeypatch):
    recorder = Recorder()
    _install(monkeypatch, recorder)

    result = send(_identity(tmp_path), TEXT)

    assert result.sent is False
    assert "missing" in result.reason
    assert str(tmp_path) in result.reason
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# The Claude call


def test_the_claude_call_s_argv_and_prompt_are_exact(tmp_path, monkeypatch):
    recorder = Recorder()
    _install(monkeypatch, recorder)
    _address(tmp_path, {"session-id": "tid", "runtime": "claude", "session-name": NAME})

    result = send(_identity(tmp_path), TEXT)

    assert result == SendResult(sent=False, reason="no SendMessage call to 'the head' with the exact text")
    (argv, kwargs) = recorder.calls[0]
    prompt = argv[2]
    assert argv[:2] == [CLAUDE, "-p"]
    assert argv[3:] == ["--tools", "SendMessage", "--allowedTools", "SendMessage",
                        "--model", "haiku", "--output-format", "stream-json",
                        "--verbose", "--strict-mcp-config"]
    assert not any("--mcp-config" in part for part in argv)
    assert f'"{NAME}"' in prompt
    assert f"\n<<<TEXT\n{TEXT}\nTEXT>>>" in prompt
    assert kwargs == dict(timeout=30, check=False, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=sys.stderr,
                          text=True, encoding="utf-8")


@pytest.mark.parametrize("mode,expected_flag", [
    ("bypassPermissions", ["--permission-mode", "bypassPermissions"]),
    (None, []),
])
def test_the_mode_flag_is_present_with_a_mode_and_absent_without(tmp_path, monkeypatch, mode, expected_flag):
    recorder = Recorder()
    _install(monkeypatch, recorder)
    payload = {"session-id": "tid", "runtime": "claude", "session-name": NAME}
    if mode is not None:
        payload["permission-mode"] = mode
    _address(tmp_path, payload)

    send(_identity(tmp_path), TEXT)

    (argv, _) = recorder.calls[0]
    assert argv[argv.index("--strict-mcp-config") + 1:] == expected_flag


def test_an_address_without_a_session_name_is_a_non_send_naming_the_field(tmp_path, monkeypatch):
    recorder = Recorder()
    _install(monkeypatch, recorder)
    _address(tmp_path, {"session-id": "tid", "runtime": "claude"})

    result = send(_identity(tmp_path), TEXT)

    assert result.sent is False
    assert "session-name" in result.reason
    assert str(tmp_path) in result.reason and OWNER in result.reason and FOLDER in result.reason
    assert recorder.calls == []


def test_a_timeout_or_a_missing_claude_is_a_non_send_raising_nothing(tmp_path, monkeypatch):
    _install(monkeypatch, Recorder(result=subprocess.TimeoutExpired(cmd=["claude"], timeout=30)))
    _address(tmp_path, {"session-id": "tid", "runtime": "claude", "session-name": NAME})

    result = send(_identity(tmp_path), TEXT)

    assert result.sent is False
    assert result.reason == "claude send timed out after 30 seconds"  # the bound, never the command
    assert TEXT not in result.reason and NAME not in result.reason

    _install(monkeypatch, Recorder(result=OSError("claude: not found")))

    result = send(_identity(tmp_path), TEXT)

    assert result.sent is False
    assert "not found" in result.reason


def test_a_claude_that_cannot_be_resolved_is_a_non_send_raising_nothing(tmp_path, monkeypatch):
    recorder = Recorder()
    _install(monkeypatch, recorder)
    monkeypatch.setattr(courier_module, "_resolve_claude",
                        lambda: (_ for _ in ()).throw(FileNotFoundError("no claude on PATH or in nvm")))
    _address(tmp_path, {"session-id": "tid", "runtime": "claude", "session-name": NAME})

    result = send(_identity(tmp_path), TEXT)

    assert result.sent is False
    assert "no claude on PATH or in nvm" in result.reason
    assert recorder.calls == []


@pytest.mark.parametrize("where", ["text", "session-id"])
def test_a_nul_character_is_a_non_send_wherever_it_sits(tmp_path, monkeypatch, where):
    _install(monkeypatch, Recorder(result=ValueError("embedded null byte")))
    if where == "text":
        _address(tmp_path, {"session-id": "tid", "runtime": "codex"})
        result = send(_identity(tmp_path), "a text with a \x00 in it")
    else:
        _address(tmp_path, {"session-id": "tid\x00", "runtime": "codex"})
        result = send(_identity(tmp_path), TEXT)

    assert result.sent is False
    assert "embedded null" in result.reason


# ---------------------------------------------------------------------------
# The Claude evidence


def _call(call_id="c1", to=NAME, message=TEXT):
    return {"type": "tool_use", "id": call_id, "name": "SendMessage",
            "input": {"to": to, "message": message}}


def _result_for(call_id="c1", payload=None, text=None):
    inner_text = text if text is not None else json.dumps(payload)
    return {"type": "tool_result", "tool_use_id": call_id,
            "content": [{"type": "text", "text": inner_text}]}


def _stream(*blocks):
    assistant = json.dumps({"type": "assistant", "message": {"content": [b for b in blocks if b.get("type") == "tool_use"]}})
    user = json.dumps({"type": "user", "message": {"content": [b for b in blocks if b.get("type") == "tool_result"]}})
    return assistant + "\n" + user + "\n"


def _claude_send(tmp_path, monkeypatch, stdout, returncode=0):
    _install(monkeypatch, Recorder(stdout=stdout, returncode=returncode))
    _address(tmp_path, {"session-id": "tid", "runtime": "claude", "session-name": NAME})
    return send(_identity(tmp_path), TEXT)


@pytest.mark.parametrize("returncode", [0, 1])
def test_a_delivered_call_is_a_send_whatever_the_exit_status(tmp_path, monkeypatch, returncode):
    stream = _stream(_call(), _result_for(payload={"success": True, "message": "delivered"}))
    result = _claude_send(tmp_path, monkeypatch, stream, returncode=returncode)
    assert result == SendResult(sent=True, reason=None)


def test_a_result_saying_no_delivery_carries_its_message(tmp_path, monkeypatch):
    stream = _stream(_call(), _result_for(payload={
        "success": False, "message": "No agent named 'the head' is reachable. Try again.",
    }))
    result = _claude_send(tmp_path, monkeypatch, stream)
    assert result.sent is False
    assert "No agent named 'the head' is reachable. Try again." in result.reason


@pytest.mark.parametrize("stream", [
    _stream(_call(message=TEXT + "x"), _result_for(payload={"success": True})),  # one character off
    _stream(_call(to="another head"), _result_for(payload={"success": True})),  # a different addressee
    _stream(_result_for(payload={"success": True})),  # no call at all
    _stream(_call()),  # a call with no result
    _stream(_call(), _result_for(text="delivered, but not JSON")),  # a result that is not JSON
    _stream(_call(), _result_for(call_id="other", payload={"success": True})),  # another call's id
])
def test_defective_evidence_is_a_non_send(tmp_path, monkeypatch, stream):
    result = _claude_send(tmp_path, monkeypatch, stream)
    assert result.sent is False
    assert result.reason


def test_lines_that_are_not_json_are_skipped(tmp_path, monkeypatch):
    stream = (
        "not json at all\n"
        + json.dumps({"type": "assistant", "message": {"content": [_call()]}})
        + "\nalso not json\n"
        + json.dumps({"type": "user", "message": {"content": [_result_for(payload={"success": True})]}})
        + "\n"
    )
    result = _claude_send(tmp_path, monkeypatch, stream)
    assert result == SendResult(sent=True, reason=None)


@pytest.mark.parametrize("call_id,result_id", [
    (None, None),  # a call with no id and a result with no tool_use_id: no id pairs them
    ("", "c1"),  # an empty-string id is not a string id
    (["c", "1"], "c1"),  # a list id is not a string id, and raises nothing
    ("c1", ["c", "1"]),  # a list tool_use_id matches nothing, and raises nothing
])
def test_only_string_ids_pair_a_call_with_its_result(tmp_path, monkeypatch, call_id, result_id):
    call = {"type": "tool_use", "name": "SendMessage",
            "input": {"to": NAME, "message": TEXT}}
    call["id"] = call_id
    result = {"type": "tool_result",
              "content": [{"type": "text", "text": json.dumps({"success": True})}]}
    result["tool_use_id"] = result_id
    stream = (json.dumps({"type": "assistant", "message": {"content": [call]}})
              + "\n"
              + json.dumps({"type": "user", "message": {"content": [result]}})
              + "\n")
    result_of_send = _claude_send(tmp_path, monkeypatch, stream)

    assert result_of_send.sent is False
    assert result_of_send.reason


def test_a_call_and_a_result_with_the_same_string_id_remain_a_send(tmp_path, monkeypatch):
    stream = _stream(_call(call_id="the-call"),
                     _result_for(call_id="the-call", payload={"success": True}))
    assert _claude_send(tmp_path, monkeypatch, stream) == SendResult(sent=True, reason=None)


def test_a_non_zero_exit_with_no_evidence_is_a_non_send(tmp_path, monkeypatch):
    result = _claude_send(tmp_path, monkeypatch, "the model refused\n", returncode=2)
    assert result.sent is False
    assert result.reason

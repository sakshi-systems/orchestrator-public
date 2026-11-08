"""Strict completion tokens, caller context, and external log isolation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from orchestrator import launcher


THREAD = "11111111-1111-4111-8111-111111111111"
INVOCATION = "22222222-2222-4222-8222-222222222222"


def git_target(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["orchestrator-launch", *args])
    launcher.cli()


@pytest.mark.parametrize("mode,evidence_root", [("implement", "../evidence"), ("test", None)])
def test_launch_and_worker_preserve_caller_context(monkeypatch, tmp_path, capsys, mode, evidence_root):
    git_target(tmp_path / "target")
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    config = caller / "claude-config.json"
    config.write_text(json.dumps({"runtime": "claude"}), encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-auth-marker")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(launcher.uuid, "uuid4", lambda: uuid.UUID(INVOCATION))
    original_environment = dict(os.environ)
    root = tmp_path / "evidence" if evidence_root else tmp_path / "home/.local/state/orchestrator/invocations"
    invocation = root / INVOCATION
    target = "../target/."
    calls = []
    returned = []
    run_logs = []

    def check_context():
        assert os.getcwd() == str(caller)
        assert dict(os.environ) == original_environment
        assert json.loads(Path(os.environ["ORCHESTRATOR_CONFIG"]).read_text()) == {"runtime": "claude"}
        assert os.environ["ANTHROPIC_API_KEY"] == "offline-auth-marker"

    def popen(argv, **kwargs):
        check_context()
        calls.append("waiter")
        assert argv == [sys.executable, "-m", "orchestrator.launcher", "_wait", mode,
                        target, "--thread", THREAD, "--invocation-id", INVOCATION,
                        "--invocation-dir", str(invocation)]
        assert kwargs == dict(cwd=str(caller), stdin=subprocess.DEVNULL,
                              stdout=kwargs["stdout"], stderr=subprocess.STDOUT,
                              start_new_session=True, close_fds=True)
        assert kwargs["stdout"].name == str(invocation / "waiter.log")
        assert kwargs["stdout"].mode == "wb" and not kwargs["stdout"].closed
        run_logs.append(kwargs["stdout"])
        return object()  # The caller neither inspects a PID nor waits for readiness.

    def run(argv, **kwargs):
        check_context()
        if argv[0] == sys.executable:
            calls.append("child")
            assert argv == [sys.executable, "-m", "orchestrator.main", mode, target,
                            "--completion-file", str(invocation / "completion.json"),
                            "--invocation-id", INVOCATION]
            assert kwargs == dict(cwd=str(caller), check=False, stdin=subprocess.DEVNULL,
                                  stdout=kwargs["stdout"], stderr=subprocess.STDOUT)
            kwargs["stdout"].write(b"configured Claude child output\n")
            (invocation / "completion.json").write_text(json.dumps({
                "invocation_id": INVOCATION, "outcome": "SUCCESS",
            }), encoding="utf-8")
        else:
            calls.append("send")
            assert argv == ["codex", "queue", "--thread", THREAD, "--message", "SUCCESS"]
            assert kwargs == dict(timeout=30, check=True, stdin=subprocess.DEVNULL,
                                  stdout=sys.stdout, stderr=subprocess.STDOUT)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(
        Popen=popen, run=run, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
        SubprocessError=subprocess.SubprocessError,
    ))
    real_launch = launcher._launch

    def launch(*args):
        result = real_launch(*args)
        assert isinstance(result, Path)
        returned.append(result)
        return result

    monkeypatch.setattr(launcher, "_launch", launch)
    extra = ["--evidence-root", evidence_root] if evidence_root else []
    invoke(monkeypatch, mode, target, "--thread", THREAD, *extra)
    assert returned == [invocation.resolve()]
    assert capsys.readouterr().out == str(invocation.resolve()) + "\n"
    assert calls == ["waiter"]
    assert run_logs[0].closed
    assert (invocation / "waiter.log").read_bytes() == b""

    launcher._wait(mode, target, THREAD, INVOCATION, returned[0])
    assert calls == ["waiter", "child", "send"]
    assert (invocation / "run.log").read_bytes() == b"configured Claude child output\n"
    assert {path.name for path in invocation.iterdir()} == {"waiter.log", "run.log", "completion.json"}


@pytest.mark.parametrize("location", ["root", "subdir", "symlink", "invocation-symlink"])
def test_rejects_evidence_inside_resolved_git_worktree(monkeypatch, tmp_path, capsys, location):
    target = git_target(tmp_path / "target")
    subdir = target / "subdir"
    subdir.mkdir()
    root = target
    if location == "subdir":
        root = subdir / "evidence"
    elif location == "symlink":
        root = tmp_path / "alias"
        root.symlink_to(subdir, target_is_directory=True)
    elif location == "invocation-symlink":
        root = tmp_path / "external"
        root.mkdir()
        (root / INVOCATION).symlink_to(subdir, target_is_directory=True)
        monkeypatch.setattr(launcher.uuid, "uuid4", lambda: uuid.UUID(INVOCATION))
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, "implement", str(subdir), "--thread", THREAD, "--evidence-root", str(root))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside target Git worktree" in captured.err


@pytest.mark.parametrize("exit_code,proof,expected", [
    (0, {"invocation_id": INVOCATION, "outcome": "SUCCESS"}, "SUCCESS"),
    (0, {"invocation_id": INVOCATION, "outcome": "FAIL"}, "FAIL"),
    (7, {"invocation_id": INVOCATION, "outcome": "SUCCESS"}, "FAIL"),
    (-15, {"invocation_id": INVOCATION, "outcome": "SUCCESS"}, "FAIL"),
    (0, {"invocation_id": THREAD, "outcome": "SUCCESS"}, "FAIL"),
    (0, None, "FAIL"),
    (0, "unreadable", "FAIL"),
    (0, b"not JSON", "FAIL"),
    (0, b"\xff", "FAIL"),
    (0, [], "FAIL"),
    (0, {"invocation_id": INVOCATION}, "FAIL"),
    (0, {"invocation_id": INVOCATION, "outcome": "SUCCESS", "extra": 1}, "FAIL"),
    (0, {"invocation_id": INVOCATION, "outcome": True}, "FAIL"),
    (0, {"invocation_id": INVOCATION, "outcome": "success"}, "FAIL"),
])
def test_actual_exit_and_exact_proof_decide_single_token(monkeypatch, tmp_path, capsys, exit_code, proof, expected):
    invocation = tmp_path / INVOCATION
    invocation.mkdir()
    proof_path = invocation / "completion.json"
    original_proof = None
    if isinstance(proof, (dict, list)):
        original_proof = json.dumps(proof).encode("utf-8")
        proof_path.write_bytes(original_proof)
    elif proof == "unreadable":
        proof_path.mkdir()
    elif proof is not None:
        original_proof = proof
        proof_path.write_bytes(original_proof)
    calls = []
    child_logs = []
    target = "./original/../target"

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            assert argv == [sys.executable, "-m", "orchestrator.main", "implement", target,
                            "--completion-file", str(proof_path), "--invocation-id", INVOCATION]
            assert kwargs == dict(cwd=os.getcwd(), check=False, stdin=subprocess.DEVNULL,
                                  stdout=kwargs["stdout"], stderr=subprocess.STDOUT)
            assert kwargs["stdout"].name == str(invocation / "run.log")
            assert kwargs["stdout"].mode == "wb" and not kwargs["stdout"].closed
            child_logs.append(kwargs["stdout"])
            kwargs["stdout"].write(b"SUCCESS FAIL are only console text\n")
            return subprocess.CompletedProcess(argv, exit_code)
        assert len(calls) == 2
        assert child_logs[0].closed
        assert (invocation / "run.log").read_bytes() == b"SUCCESS FAIL are only console text\n"
        assert argv == ["codex", "queue", "--thread", THREAD, "--message", expected]
        assert kwargs == dict(timeout=30, check=True, stdin=subprocess.DEVNULL,
                              stdout=sys.stdout, stderr=subprocess.STDOUT)
        print("queue output", file=kwargs["stdout"])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(
        run=run, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
        SubprocessError=subprocess.SubprocessError,
    ))
    launcher._wait("implement", target, THREAD, INVOCATION, invocation)
    assert len(calls) == 2
    captured = capsys.readouterr()
    assert captured.out == "queue output\n"
    assert f"return code: {exit_code}" in captured.err
    assert f"Completion token: {expected}" in captured.err
    if original_proof is not None:
        assert proof_path.read_bytes() == original_proof
    elif proof == "unreadable":
        assert proof_path.is_dir()
    else:
        assert not proof_path.exists()
    expected_files = {"run.log", "completion.json"} if proof is not None else {"run.log"}
    assert {path.name for path in invocation.iterdir()} == expected_files

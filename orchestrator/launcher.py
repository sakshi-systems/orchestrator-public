"""Detached ordinary-CLI ownership and one retained completion delivery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .layout import _external_evidence_path


def _canonical_uuid(value: str) -> str:
    try:
        if value != str(uuid.UUID(value)):
            raise ValueError("noncanonical UUID")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a canonical lowercase hyphenated UUID") from exc
    return value


def _read_proof(path: Path, invocation_id: str) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")) == {
            "invocation_id": invocation_id, "outcome": "SUCCESS",
        }
    except (OSError, ValueError):
        return False


def _wait(mode: str, target: str, thread_id: str, invocation_id: str, invocation_dir: Path) -> None:
    token = "FAIL"
    with (invocation_dir / "run.log").open("wb") as run_log:
        try:
            child = subprocess.run(
                [sys.executable, "-m", "orchestrator.main", mode, target,
                 "--completion-file", str(invocation_dir / "completion.json"),
                 "--invocation-id", invocation_id],
                cwd=os.getcwd(), check=False, stdin=subprocess.DEVNULL,
                stdout=run_log, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            print(f"Could not start ordinary CLI child: {exc}", file=sys.stderr, flush=True)
        else:
            print(f"Ordinary CLI child return code: {child.returncode}", file=sys.stderr, flush=True)
            if child.returncode == 0 and _read_proof(invocation_dir / "completion.json", invocation_id):
                token = "SUCCESS"

    print(f"Completion token: {token}", file=sys.stderr, flush=True)
    try:
        subprocess.run(
            ["codex", "queue", "--thread", thread_id, "--message", token],
            timeout=30, check=True, stdin=subprocess.DEVNULL,
            stdout=sys.stdout, stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Queue delivery failed: {exc}", file=sys.stderr, flush=True)


def _launch(mode: str, target: str, thread_id: str, evidence_root: str | None) -> Path:
    caller_cwd = os.getcwd()
    root = Path(evidence_root) if evidence_root is not None else (
        Path.home() / ".local/state/orchestrator/invocations"
    )
    root = _external_evidence_path(Path(target), root)
    invocation_id = str(uuid.uuid4())
    invocation_dir = _external_evidence_path(Path(target), root / invocation_id)
    root.mkdir(parents=True, exist_ok=True)
    invocation_dir.mkdir()
    with (invocation_dir / "waiter.log").open("wb") as waiter_log:
        subprocess.Popen(
            [sys.executable, "-m", "orchestrator.launcher", "_wait", mode, target,
             "--thread", thread_id, "--invocation-id", invocation_id,
             "--invocation-dir", str(invocation_dir)],
            cwd=caller_cwd, stdin=subprocess.DEVNULL, stdout=waiter_log,
            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
        )
    return invocation_dir


def cli() -> None:
    if sys.argv[1:2] == ["_wait"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("mode", choices=("implement", "test"))
        parser.add_argument("target")
        parser.add_argument("--thread", required=True, type=_canonical_uuid)
        parser.add_argument("--invocation-id", required=True, type=_canonical_uuid)
        parser.add_argument("--invocation-dir", required=True, type=Path)
        args = parser.parse_args(sys.argv[2:])
        _wait(args.mode, args.target, args.thread, args.invocation_id, args.invocation_dir)
        return

    parser = argparse.ArgumentParser(description="Launch a detached orchestrator run")
    parser.add_argument("mode", choices=("implement", "test"))
    parser.add_argument("target")
    parser.add_argument("--thread", required=True, type=_canonical_uuid)
    parser.add_argument("--evidence-root")
    args = parser.parse_args()
    try:
        invocation_dir = _launch(args.mode, args.target, args.thread, args.evidence_root)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(invocation_dir, flush=True)


if __name__ == "__main__":
    cli()

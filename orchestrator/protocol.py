"""The file protocol: sidecar read/write and completion-signal and escalation recognition."""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .config import AGENT_RUNTIMES
from .errors import EmptyAnswerError, HaltError
from .ports import AgentRole

PLAN_REVIEW_PASS = "PLAN_REVIEW_PASS"


def _write_artifact(path: Path, answer: str) -> None:
    """Write an agent's answer unchanged to `path` in UTF-8, creating the parent
    directories.

    Raises EmptyAnswerError before anything is written when the answer is empty.
    Writes directly — it does not go through os.replace, so it never crosses the
    sidecar writer's atomic replacement boundary.
    """
    if not answer.strip():
        raise EmptyAnswerError(f"Agent returned an empty answer\n{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(answer, encoding="utf-8")


def _read_artifact(path: Path) -> str:
    """Read an artifact the run wrote back as UTF-8, whatever the machine's locale."""
    return path.read_text(encoding="utf-8")


def _has_signal(text: str, signal: str) -> bool:
    """Return True if signal appears as an exact line within the last 5 lines."""
    return any(line.strip() == signal for line in text.splitlines()[-5:])


def _has_escalation(text: str) -> bool:
    """Return True if the ESCALATION marker appears as an exact line within the last 5 lines."""
    return _has_signal(text, "ESCALATION")


def _escalation_excerpt(text: str) -> str:
    """Return the first non-empty, non-heading line under a `## Escalation` section.

    Returns "" if the section is absent (placeholder for the sidecar summary)."""
    in_section = False
    for line in text.splitlines():
        if line.strip() == "## Escalation":
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if stripped.startswith("#"):
                break
            if stripped:
                return stripped
    return ""


def _load_sidecar(p: Path) -> dict[str, Any]:
    """Read the sidecar record at `p`.

    An absent file reads as `{}`; an existing sidecar that cannot be read,
    is not UTF-8, is not valid JSON, or is not a JSON object halts, so its
    contents are never mistaken for an empty record."""
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise HaltError(f"Sidecar could not be read\n{p}\n{e}") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HaltError(f"Sidecar is not valid UTF-8 text\n{p}\n{e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HaltError(f"Sidecar is not valid JSON\n{p}\n{e}") from e
    if not isinstance(data, dict):
        raise HaltError(f"Sidecar JSON is not an object\n{p}\n{type(data).__name__}")
    return data


def _is_role_runtime_entry(entry: object) -> bool:
    """True for a role's entry: a local runtime, Hermes with the instance's
    name, or a head with its address."""
    if not isinstance(entry, dict):
        return False
    runtime = entry.get("runtime")
    if runtime == "hermes":
        return isinstance(entry.get("instance"), str) and bool(entry["instance"].strip())
    if runtime == "head":
        head = entry.get("head")
        return (isinstance(head, dict) and set(head) == {"repository", "owner", "folder"}
                and all(isinstance(head[name], str) and head[name]
                        for name in ("repository", "owner", "folder")))
    return isinstance(runtime, str) and runtime in AGENT_RUNTIMES


def _is_role_runtime_record(value: object) -> bool:
    """True for the run's owner record: an entry per role of the four, each naming a known runtime.

    A role the record leaves out is valid — resume reads that role as taking its
    effective entry. The legacy single-string owner is accepted beside this form.
    """
    if not isinstance(value, dict):
        return False
    if not set(value) <= {role.value for role in AgentRole}:
        return False
    return all(_is_role_runtime_entry(entry) for entry in value.values())


def _validate_record(data: dict[str, Any], path: Path) -> None:
    """Check known values without normalizing identities or optional metadata."""
    for key, value in data.items():
        valid = True
        if key == "runtime":
            valid = (isinstance(value, str) and value in AGENT_RUNTIMES) or _is_role_runtime_record(value)
        elif key in ("planner", "implementer", "code-reviewer") or re.fullmatch(r"plan-reviewer-[0-9]+", key):
            valid = isinstance(value, str) and bool(value.strip())
        elif key == "elapsed":
            valid = isinstance(value, str) and bool(value) and value.isascii() and value.isdecimal()
        elif key == "step":
            valid = isinstance(value, str)
        if not valid:
            raise HaltError(f"Sidecar has an invalid field value\n{path}\n{key}: {value!r}")


def _read_sessions(plan_path: Path) -> dict[str, Any]:
    """Return the plan's sidecar record: `{}` when absent; halts when the existing
    sidecar is unreadable, non-UTF-8, malformed or not a JSON object."""
    p = plan_path.with_suffix('.json')
    data = _load_sidecar(p)
    _validate_record(data, p)
    return data


def _write_session(plan_path: Path, key: str, value: str) -> None:
    """Merge one key through the shared atomic record writer."""
    _write_sessions(plan_path, {key: value})


def _write_sessions(plan_path: Path, updates: dict[str, Any]) -> None:
    """Merge keys into the existing sidecar record via a temporary file and an
    atomic replace. Invalid existing data or an I/O failure halts without
    replacing the original."""
    p = plan_path.with_suffix('.json')
    data = _read_sessions(plan_path)
    data.update(updates)
    _validate_record(data, p)
    tmp = p.with_suffix('.json.tmp')
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise HaltError(f"Sidecar could not be updated\n{p}\n{e}") from e

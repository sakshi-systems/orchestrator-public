"""The placement of a head exchange: the one home of the message, the marker
and the answer files, outside every project's tree."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from .ports import AgentRole

POLL_SECONDS = 2

_ROOT = Path.home() / ".local/state/orchestrator/exchanges"
_DIRS = ("plans", "plan-reviews", "reviews")


def answer_command() -> Path:
    """The answer command's path: the directory of the running interpreter
    joined with `orchestrator-answer`, the one home of the recipe."""
    return Path(sys.executable).parent / "orchestrator-answer"


class ExchangePaths(NamedTuple):
    """A call's three files: the message sent, the marker of the send and the deposited answer."""

    message: Path
    sent: Path
    answer: Path


def _digest_of(project_dir: Path) -> str:
    """The SHA-256 hexadecimal digest of the project's resolved path."""
    return hashlib.sha256(str(project_dir.resolve()).encode("utf-8")).hexdigest()


def exchange_paths(project_dir: Path, role: AgentRole, exchange: str) -> ExchangePaths:
    """A call's three files under the project's exchange directory.

    The directory is `~/.local/state/orchestrator/exchanges/<digest>/`, the
    digest of the project's resolved path; the files sit in the exchange's
    directory part and are named `<role value>-<last segment>` with the
    suffixes `.message.md`, `.sent` and `.answer`.
    """
    directory, _, last = exchange.rpartition("/")
    base = _ROOT / _digest_of(project_dir) / directory
    stem = f"{role.value}-{last}"
    return ExchangePaths(
        message=base / f"{stem}.message.md",
        sent=base / f"{stem}.sent",
        answer=base / f"{stem}.answer",
    )


def deposit(answer_path: Path, text: str) -> None:
    """Deposit an answer so that it appears whole: a temporary sibling is
    written and linked into place, and a deposit that exists is left as it is,
    raising FileExistsError. The temporary file is removed in any case."""
    handle, temporary = tempfile.mkstemp(dir=str(answer_path.parent), prefix=".deposit-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as deposited:
            deposited.write(text)
        os.link(temporary, answer_path)
    finally:
        os.unlink(temporary)


def remove_call(paths: ExchangePaths) -> None:
    """Remove a call's three files, ignoring one that is missing."""
    for path in (paths.message, paths.sent, paths.answer):
        path.unlink(missing_ok=True)


def remove_task(project_dir: Path, plan_path: Path) -> None:
    """Remove the task's exchange files under its roadmap's artifact segment.

    In the exchange's `plans`, `plan-reviews` and `reviews` directories under
    the segment — `plan_path`'s directory relative to `<project>/.ai-factory/
    plans` — every file named `<role value>-<plan stem>` followed by `.` or
    `-`, for each of the run's roles, and nothing else. A directory that does
    not exist is not an error, and nothing that does not exist is removed.
    """
    root = _ROOT / _digest_of(project_dir)
    segment = plan_path.parent.relative_to(project_dir / ".ai-factory" / "plans")
    stem = plan_path.stem
    prefixes = tuple(
        (f"{role.value}-{stem}.", f"{role.value}-{stem}-") for role in AgentRole
    )
    for name in _DIRS:
        directory = root / name
        if segment.parts:
            directory = directory.joinpath(*segment.parts)
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if any(entry.name.startswith(prefix) for pair in prefixes for prefix in pair):
                entry.unlink(missing_ok=True)

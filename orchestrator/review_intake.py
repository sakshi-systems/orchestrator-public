"""The intake that decides a review request and records it once per
head commit.

Standard library only, beside ``reviews``, whose request form and
repository spellings it decides by, and ``sakshi_settings``, whose
teams it hands on. The ledger of record files is the intake's own and
no other module touches it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from .reviews import (
    ReviewAnswer,
    ReviewRefusal,
    ReviewRequest,
    is_head_commit,
    is_number,
    is_repository,
    same_repository,
)
from .sakshi_settings import ReviewedRepository, ReviewTeam, SakshiSettings

_RECORD_KEYS = {"state", "roles", "musafir"}


class ReviewState(StrEnum):
    """A review's state, as the ledger records it."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPOSING = "composing"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewRecord:
    """One review's ledger entry: its state, the session ids of its
    roles, and the session id of its musafir."""

    state: ReviewState
    roles: dict[str, str] = field(default_factory=dict)
    musafir: str | None = None


class AcceptedReview(NamedTuple):
    """An accepted request with the team that reviews it and the
    checkout the review works from."""

    request: ReviewRequest
    team: ReviewTeam
    checkout: Path


def _record_json(record: ReviewRecord) -> dict[str, object]:
    return {
        "state": record.state.value,
        "roles": dict(record.roles),
        "musafir": record.musafir,
    }


def _record_from(data: object) -> ReviewRecord | None:
    if not isinstance(data, dict) or set(data) != _RECORD_KEYS:
        return None
    state = data["state"]
    if not isinstance(state, str):
        return None
    try:
        state = ReviewState(state)
    except ValueError:
        return None
    roles = data["roles"]
    if not isinstance(roles, dict):
        return None
    for name, session in roles.items():
        if not isinstance(name, str) or not isinstance(session, str):
            return None
    musafir = data["musafir"]
    if musafir is not None and not isinstance(musafir, str):
        return None
    return ReviewRecord(state=state, roles=roles,
                        musafir=musafir)


class ReviewIntake:
    """Decides review requests against the settings and records each
    review once per head commit, handing an accepted review on. The
    ledger lives under the given directory and no other module touches
    its files."""

    def __init__(self, settings: SakshiSettings, directory: Path,
                 on_accepted: Callable[[AcceptedReview], None]) -> None:
        self._settings = settings
        self._directory = directory
        self._on_accepted = on_accepted
        self._stopped = False

    def stop(self) -> None:
        """Refuse every later request: a stop is in progress."""
        self._stopped = True

    def admit(self, request: ReviewRequest) -> ReviewAnswer:
        """Decide one request, recording the review before it is handed
        on, so a crash in between loses the review for good and it is
        never raised again."""
        if self._stopped:
            return ReviewAnswer(refusal=ReviewRefusal.STOPPING)
        repository = self._reviewed(request.repository)
        if repository is None:
            return ReviewAnswer(
                refusal=ReviewRefusal.REPOSITORY_NOT_REVIEWED)
        if not same_repository(request.head_repository, request.repository):
            return ReviewAnswer(
                refusal=ReviewRefusal.HEAD_FROM_ANOTHER_REPOSITORY)
        team = repository.team
        if not team.roles or team.musafir is None:
            return ReviewAnswer(refusal=ReviewRefusal.ROLES_INCOMPLETE)
        path = self._record_path(request)
        if path.exists():
            return ReviewAnswer(refusal=None)
        self._write(path, ReviewRecord(state=ReviewState.ACCEPTED))
        self._on_accepted(AcceptedReview(request=request, team=team,
                                         checkout=repository.checkout))
        return ReviewAnswer(refusal=None)

    def record_of(self, request: ReviewRequest) -> ReviewRecord | None:
        """The review's record, or None when there is no record file or
        the file is not a valid record."""
        path = self._record_path(request)
        try:
            if not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            return None
        return _record_from(data)

    def update(self, request: ReviewRequest, record: ReviewRecord) -> None:
        """Replace the review's record with the given one."""
        self._write(self._record_path(request), record)

    def _reviewed(self, repository: str) -> ReviewedRepository | None:
        for key, entry in self._settings.repositories.items():
            if same_repository(key, repository):
                return entry
        return None

    def _record_path(self, request: ReviewRequest) -> Path:
        if (type(request.repository) is not str
                or not is_repository(request.repository)
                or not is_number(request.number)
                or not is_head_commit(request.head_commit)):
            raise ValueError(f"request names no record: {request!r}")
        owner, _, name = request.repository.partition("/")
        return (self._directory / "reviews" / owner.casefold()
                / name.casefold() / str(request.number)
                / f"{request.head_commit}.json")

    def _write(self, path: Path, record: ReviewRecord) -> None:
        self._make_directories(path.parent)
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent),
                                                 prefix=".record-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
                json.dump(_record_json(record), sink)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _make_directories(self, record: Path) -> None:
        current = self._directory
        if not current.exists():
            current.mkdir(mode=0o700)
            os.chmod(current, 0o700)
        for part in record.relative_to(current).parts:
            current = current / part
            if not current.exists():
                current.mkdir(mode=0o700)
                os.chmod(current, 0o700)

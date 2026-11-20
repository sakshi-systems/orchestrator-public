"""A request to review a pull request as one validated form, with the
refusals a sender branches on, by meaning.

Standard library only; imports nothing of the run, the flow or Sakshi's
root. The process takes a request and knows nothing of how the pull
request was noticed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import NamedTuple

_OWNER = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
_NAME = r"[A-Za-z0-9._-]+"
_COMMIT = r"[0-9a-f]{40}"
_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_FIELDS = ("repository", "number", "head_commit", "head_repository")


class ReviewRequest(NamedTuple):
    """One request to review a pull request, as the request names it."""

    repository: str
    number: int
    head_commit: str
    head_repository: str


class ReviewRefusal(StrEnum):
    """The typed refusals of a review request.

    The values are the contract a sender branches on, so they are part
    of the boundary and never respelled.
    """

    #: A field is missing, unknown or outside its form.
    INVALID_REQUEST = "invalid-request"
    #: The repository is not among those the settings review.
    REPOSITORY_NOT_REVIEWED = "repository-not-reviewed"
    #: The head branch lives in another repository.
    HEAD_FROM_ANOTHER_REPOSITORY = "head-from-another-repository"
    #: The repository's roles or musafir are incomplete in the settings.
    ROLES_INCOMPLETE = "roles-incomplete"
    #: A stop is in progress.
    STOPPING = "stopping"


class ReviewFailureReason(StrEnum):
    """The reasons a review fails.

    The values are the contract a reader of the record and of the
    reference branches on, so they are part of the boundary and never
    respelled.
    """

    #: No role wrote its report into the reviews directory.
    NO_ROLE_REPORTED = "no-role-reported"
    #: The musafir, the liaison with the outside world, gave no output.
    MUSAFIR_GAVE_NO_OUTPUT = "musafir-gave-no-output"
    #: A stop cut the review short.
    STOP_CUT_THE_REVIEW = "stop-cut-the-review"
    #: The review's workspace could not be laid.
    WORKSPACE_NOT_LAID = "workspace-not-laid"
    #: Sakshi could not write the review's files: a report into the
    #: workspace or the record of the review.
    STORAGE_FAILED = "storage-failed"


class ReviewRefused(Exception):
    """A refused review request, carried by meaning.

    The message is the refusal's value, followed by ": " and the detail
    when there is one.
    """

    def __init__(self, refusal: ReviewRefusal, detail: str | None = None) -> None:
        self.refusal = refusal
        self.detail = detail
        if detail:
            super().__init__(f"{refusal}: {detail}")
        else:
            super().__init__(f"{refusal}")


class ReviewAnswer(NamedTuple):
    """The answer to a review request: a refusal, or none when the
    request is accepted."""

    refusal: ReviewRefusal | None

    @property
    def accepted(self) -> bool:
        return self.refusal is None


def is_repository(value: object) -> bool:
    """True when value spells a repository as owner/name: the owner 1
    to 39 characters of [A-Za-z0-9] joined by single hyphens, the name 1
    to 100 characters of [A-Za-z0-9._-] that is neither "." nor ".."."""
    if not isinstance(value, str):
        return False
    owner, sep, name = value.partition("/")
    if not sep or "/" in name:
        return False
    if name in (".", ".."):
        return False
    if not 1 <= len(owner) <= 39:
        return False
    if not 1 <= len(name) <= 100:
        return False
    return (
        re.fullmatch(_OWNER, owner) is not None
        and re.fullmatch(_NAME, name) is not None
    )


def is_slug(value: object) -> bool:
    """True when value is a slug: lowercase [a-z0-9] runs joined by
    single hyphens, 1 to 64 characters. The settings reuse this."""
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= 64:
        return False
    return re.fullmatch(_SLUG, value) is not None


def is_head_commit(value: object) -> bool:
    """True when value is a str of exactly 40 characters of lowercase
    hexadecimal, and no subclass of str."""
    if type(value) is not str:
        return False
    return re.fullmatch(_COMMIT, value) is not None


def is_number(value: object) -> bool:
    """True when value is an int from 1 to 2147483647 inclusive, and no
    subclass of int, so a bool is not a number."""
    if type(value) is not int:
        return False
    return 1 <= value <= 2147483647


USER_NAME = r"[a-z_][a-z0-9_-]{0,30}"


def is_user_name(value: object) -> bool:
    """True when value is an exact str naming an OS user: a lowercase
    letter or an underscore first, then lowercase letters, digits,
    underscores and hyphens, at most 31 characters, and no subclass of
    str. The settings and the runtime's user check reuse this."""
    if type(value) is not str:
        return False
    return re.fullmatch(USER_NAME, value) is not None


def same_repository(a: str, b: str) -> bool:
    """True when casefold() makes the two spellings equal, so two
    spellings that differ only in case name one repository."""
    return a.casefold() == b.casefold()


def parse_review_request(fields: Mapping[str, object]) -> ReviewRequest:
    """Read one review request, refusing an invalid one by meaning with
    the key in the detail.

    No value is converted: a string stays a string, an integer an
    integer.
    """
    if not isinstance(fields, Mapping):
        raise ReviewRefused(ReviewRefusal.INVALID_REQUEST, "not an object")
    for key in fields:
        if key not in _FIELDS:
            raise ReviewRefused(ReviewRefusal.INVALID_REQUEST,
                                f"unknown field: {key}")
    for key in _FIELDS:
        if key not in fields:
            raise ReviewRefused(ReviewRefusal.INVALID_REQUEST,
                                f"missing field: {key}")
    repository = fields["repository"]
    if not isinstance(repository, str) or not is_repository(repository):
        raise ReviewRefused(ReviewRefusal.INVALID_REQUEST,
                            "invalid field: repository")
    number = fields["number"]
    if not is_number(number):
        raise ReviewRefused(ReviewRefusal.INVALID_REQUEST,
                            "invalid field: number")
    head_commit = fields["head_commit"]
    if not is_head_commit(head_commit):
        raise ReviewRefused(ReviewRefusal.INVALID_REQUEST,
                            "invalid field: head_commit")
    head_repository = fields["head_repository"]
    if not isinstance(head_repository, str) or not is_repository(head_repository):
        raise ReviewRefused(ReviewRefusal.INVALID_REQUEST,
                            "invalid field: head_repository")
    return ReviewRequest(
        repository=repository,
        number=number,
        head_commit=head_commit,
        head_repository=head_repository,
    )

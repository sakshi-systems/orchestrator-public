"""A review request's validated form and the refusals a sender branches
on."""

import pytest

from orchestrator.reviews import (
    ReviewAnswer,
    ReviewFailureReason,
    ReviewRefusal,
    ReviewRefused,
    ReviewRequest,
    is_repository,
    is_slug,
    parse_review_request,
    same_repository,
)

COMMIT = "abcdef0123456789abcdef0123456789abcdef12"


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "repository": "owner/name",
        "number": 1,
        "head_commit": COMMIT,
        "head_repository": "owner/name",
    }
    fields.update(overrides)
    return fields


REFUSAL_VALUES = [
    (ReviewRefusal.INVALID_REQUEST, "invalid-request"),
    (ReviewRefusal.REPOSITORY_NOT_REVIEWED, "repository-not-reviewed"),
    (ReviewRefusal.HEAD_FROM_ANOTHER_REPOSITORY,
     "head-from-another-repository"),
    (ReviewRefusal.ROLES_INCOMPLETE, "roles-incomplete"),
    (ReviewRefusal.STOPPING, "stopping"),
]


@pytest.mark.parametrize(("member", "value"), REFUSAL_VALUES)
def test_refusal_value(member: ReviewRefusal, value: str) -> None:
    assert member.value == value
    assert str(member) == value
    # A sender branches on the raw string.
    assert member == value


FAILURE_REASON_VALUES = [
    (ReviewFailureReason.NO_ROLE_REPORTED, "no-role-reported"),
    (ReviewFailureReason.MUSAFIR_GAVE_NO_OUTPUT,
     "musafir-gave-no-output"),
    (ReviewFailureReason.STOP_CUT_THE_REVIEW, "stop-cut-the-review"),
    (ReviewFailureReason.WORKSPACE_NOT_LAID, "workspace-not-laid"),
    (ReviewFailureReason.STORAGE_FAILED, "storage-failed"),
]


@pytest.mark.parametrize(("member", "value"), FAILURE_REASON_VALUES)
def test_failure_reason_value(member: ReviewFailureReason,
                              value: str) -> None:
    assert member.value == value
    assert str(member) == value
    # A reader of the record and of the reference branches on the raw
    # string.
    assert member == value


def test_refused_carries_refusal_and_detail() -> None:
    error = ReviewRefused(ReviewRefusal.ROLES_INCOMPLETE,
                          "the settings name no musafir")
    assert error.refusal is ReviewRefusal.ROLES_INCOMPLETE
    assert error.detail == "the settings name no musafir"
    assert str(error) == "roles-incomplete: the settings name no musafir"


def test_refused_without_detail() -> None:
    error = ReviewRefused(ReviewRefusal.STOPPING)
    assert error.refusal is ReviewRefusal.STOPPING
    assert error.detail is None
    assert str(error) == "stopping"


def test_the_answer_with_no_refusal_is_accepted() -> None:
    assert ReviewAnswer(refusal=None).accepted is True


def test_the_answer_with_a_refusal_is_not_accepted() -> None:
    answer = ReviewAnswer(refusal=ReviewRefusal.REPOSITORY_NOT_REVIEWED)
    assert answer.refusal is ReviewRefusal.REPOSITORY_NOT_REVIEWED
    assert answer.accepted is False


def test_answer_refusal_has_no_default() -> None:
    with pytest.raises(TypeError):
        ReviewAnswer()


REPOSITORY_SPELLINGS = [
    ("a/b", True),
    ("a" * 39 + "/name", True),
    ("a" * 40 + "/name", False),
    ("-owner/name", False),
    ("owner-/name", False),
    ("ow--ner/name", False),
    ("owner/" + "n" * 100, True),
    ("owner/" + "n" * 101, False),
    ("owner/.", False),
    ("owner/..", False),
    ("owner/.github", True),
]


@pytest.mark.parametrize(("value", "expected"), REPOSITORY_SPELLINGS)
def test_is_repository(value: str, expected: bool) -> None:
    assert is_repository(value) is expected


SLUGS = [
    ("team", True),
    ("a" * 64, True),
    ("a" * 65, False),
    ("a-b-c", True),
    ("-a", False),
    ("a-", False),
    ("a--b", False),
    ("Team", False),
    ("a_b", False),
]


@pytest.mark.parametrize(("value", "expected"), SLUGS)
def test_is_slug(value: str, expected: bool) -> None:
    assert is_slug(value) is expected


LOOKALIKE_SPELLINGS = [
    "\u00f6wner/name",
    "own\u0430r/name",
    "\uff10wner/name",
    "owner/na\u200bme",
    "owner/na\u2028me",
    "owner/na\x00me",
    "owner/na@me",
    "owner/na%me",
    "owner/na:me",
    "owner/na me",
]


@pytest.mark.parametrize("value", LOOKALIKE_SPELLINGS)
def test_is_repository_refuses_lookalikes(value: str) -> None:
    assert is_repository(value) is False


@pytest.mark.parametrize("value", [None, 5, ["owner/name"], b"owner/name"])
def test_is_repository_refuses_non_strings(value: object) -> None:
    assert is_repository(value) is False


@pytest.mark.parametrize("value", [None, 5, ["team"], b"team"])
def test_is_slug_refuses_non_strings(value: object) -> None:
    assert is_slug(value) is False


def test_same_repository_folds_case() -> None:
    assert same_repository("Sakshi-Systems/Orchestrator",
                           "sakshi-systems/orchestrator") is True
    assert same_repository("sakshi-systems/orchestrator",
                           "sakshi-systems/other") is False


class _Commit(str):
    """A str subclass that spells a commit."""


class _Number(int):
    """An int subclass inside the bound."""


INVALID_FIELDS = [
    (_fields(repository="a" * 40 + "/name"), "invalid field: repository"),
    (_fields(repository="-owner/name"), "invalid field: repository"),
    (_fields(repository="owner-/name"), "invalid field: repository"),
    (_fields(repository="ow--ner/name"), "invalid field: repository"),
    (_fields(repository="owner/" + "n" * 101), "invalid field: repository"),
    (_fields(repository="owner/."), "invalid field: repository"),
    (_fields(repository="owner/.."), "invalid field: repository"),
    (_fields(number=0), "invalid field: number"),
    (_fields(number=-1), "invalid field: number"),
    (_fields(number=True), "invalid field: number"),
    (_fields(number="5"), "invalid field: number"),
    (_fields(number=5.0), "invalid field: number"),
    (_fields(number=2147483648), "invalid field: number"),
    (_fields(number=_Number(5)), "invalid field: number"),
    (_fields(head_commit="a" * 39), "invalid field: head_commit"),
    (_fields(head_commit="a" * 41), "invalid field: head_commit"),
    (_fields(head_commit="A" * 40), "invalid field: head_commit"),
    (_fields(head_commit="g" * 40), "invalid field: head_commit"),
    (_fields(head_commit=_Commit("a" * 40)), "invalid field: head_commit"),
    (_fields(head_repository="a" * 40 + "/name"),
     "invalid field: head_repository"),
    (_fields(head_repository="ow--ner/name"),
     "invalid field: head_repository"),
    (_fields(head_repository="owner/.."),
     "invalid field: head_repository"),
]

_STRING_FIELD_BASES = {
    "repository": "owner/name",
    "head_commit": COMMIT,
    "head_repository": "owner/name",
}
WHITESPACE = []
for _field, _base in _STRING_FIELD_BASES.items():
    _middle = len(_base) // 2
    WHITESPACE += [
        (_fields(**{_field: _base + "\n"}), f"invalid field: {_field}"),
        (_fields(**{_field: _base + " "}), f"invalid field: {_field}"),
        (_fields(**{_field: _base[:_middle] + "\n" + _base[_middle:]}),
         f"invalid field: {_field}"),
    ]

MISSING = []
for _field in ("repository", "number", "head_commit", "head_repository"):
    _broken = _fields()
    del _broken[_field]
    MISSING.append((_broken, f"missing field: {_field}"))

UNKNOWN = [
    (_fields(team="architects"), "unknown field: team"),
    (_fields(url="https://example.invalid/pull/7"), "unknown field: url"),
]

NOT_OBJECTS = [
    (["owner/name", 1, COMMIT, "owner/name"], "not an object"),
    ("owner/name", "not an object"),
]

LOOKALIKE_REQUESTS = [
    (_fields(repository=value), "invalid field: repository")
    for value in LOOKALIKE_SPELLINGS
] + [
    (_fields(head_repository=value), "invalid field: head_repository")
    for value in LOOKALIKE_SPELLINGS
]

_FIELD_RIGHT_STRINGS = {
    "repository": "owner/name",
    "head_commit": COMMIT,
    "head_repository": "owner/name",
}
WRONG_TYPE_REQUESTS = []
for _field, _right in _FIELD_RIGHT_STRINGS.items():
    for _wrong in (None, 5, [_right], _right.encode()):
        WRONG_TYPE_REQUESTS.append((_fields(**{_field: _wrong}),
                                    f"invalid field: {_field}"))

INVALID_REQUESTS = (INVALID_FIELDS + LOOKALIKE_REQUESTS
                    + WRONG_TYPE_REQUESTS + WHITESPACE + MISSING + UNKNOWN
                    + NOT_OBJECTS)


@pytest.mark.parametrize(("fields", "detail"), INVALID_REQUESTS)
def test_invalid_request_is_refused_by_meaning(
    fields: object, detail: str
) -> None:
    with pytest.raises(ReviewRefused) as raised:
        parse_review_request(fields)
    assert raised.value.refusal is ReviewRefusal.INVALID_REQUEST
    assert str(raised.value) == f"invalid-request: {detail}"


ACCEPTED = [
    ("sakshi-systems/orchestrator", 1, COMMIT, "sakshi-systems/orchestrator"),
    ("a" * 39 + "/" + "n" * 100, 2, COMMIT, "owner/.github"),
    ("Owner/Name", 2147483647, COMMIT, "OWNER/name"),
]


@pytest.mark.parametrize(
    ("repository", "number", "head_commit", "head_repository"), ACCEPTED
)
def test_parse_reads_the_form_as_sent(
    repository: str,
    number: int,
    head_commit: str,
    head_repository: str,
) -> None:
    request = parse_review_request(
        _fields(repository=repository, number=number,
                head_commit=head_commit, head_repository=head_repository)
    )
    assert request == ReviewRequest(
        repository=repository,
        number=number,
        head_commit=head_commit,
        head_repository=head_repository,
    )

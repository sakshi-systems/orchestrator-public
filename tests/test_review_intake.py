"""The review intake: what it refuses, what it records once per head
commit, and what it hands on."""

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.review_intake import (
    AcceptedReview,
    ReviewIntake,
    ReviewRecord,
    ReviewState,
)
from orchestrator.reviews import ReviewAnswer, ReviewRefusal, ReviewRequest
from orchestrator.sakshi_settings import (
    ReviewedRepository,
    ReviewTeam,
    RoleSettings,
    SakshiSettings,
)

COMMIT = "abcdef0123456789abcdef0123456789abcdef12"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"

STATES = [
    (ReviewState.ACCEPTED, "accepted"),
    (ReviewState.RUNNING, "running"),
    (ReviewState.COMPOSING, "composing"),
    (ReviewState.FINISHED, "finished"),
    (ReviewState.FAILED, "failed"),
]


@pytest.mark.parametrize(("member", "value"), STATES)
def test_state_value(member: ReviewState, value: str) -> None:
    assert member.value == value
    assert str(member) == value
    # A record's file holds the raw string.
    assert member == value


def _role() -> RoleSettings:
    return RoleSettings(seed=Path("/srv/seeds/architect"),
                        user="review_arch", model="claude-3",
                        effort="high", head="01")


def _team(*, roles: bool = True, musafir: bool = True) -> ReviewTeam:
    return ReviewTeam(
        memory=Path("/srv/memory/name"),
        deadline_seconds=30,
        roles={"architect": _role()} if roles else {},
        musafir=_role() if musafir else None,
    )


def _settings(key: str = "owner/name",
              team: ReviewTeam | None = None) -> SakshiSettings:
    return SakshiSettings(
        workspace_root=Path("/srv/sakshi"),
        repositories={key: ReviewedRepository(
            checkout=Path("/srv/checkouts/name"),
            team=team if team is not None else _team())},
    )


def _request(**overrides: object) -> ReviewRequest:
    fields: dict[str, object] = dict(repository="owner/name", number=1,
                                     head_commit=COMMIT,
                                     head_repository="owner/name")
    fields.update(overrides)
    return ReviewRequest(**fields)


def _record_file(tmp_path: Path, commit: str = COMMIT,
                 number: int = 1) -> Path:
    return (tmp_path / "reviews" / "owner" / "name" / str(number)
            / f"{commit}.json")


def _two_settings() -> SakshiSettings:
    return SakshiSettings(
        workspace_root=Path("/srv/sakshi"),
        repositories={
            "owner/first": ReviewedRepository(
                checkout=Path("/srv/checkouts/first"),
                team=_team()),
            "owner/second": ReviewedRepository(
                checkout=Path("/srv/checkouts/second"),
                team=ReviewTeam(
                    memory=Path("/srv/memory/second"),
                    deadline_seconds=60,
                    roles={"auditor": _role()},
                    musafir=_role())),
        },
    )


def _refused(tmp_path: Path, settings: SakshiSettings, request: ReviewRequest,
             refusal: ReviewRefusal) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(settings, tmp_path, calls.append)
    answer = intake.admit(request)
    assert answer == ReviewAnswer(refusal=refusal)
    assert calls == []
    assert not (tmp_path / "reviews").exists()


def test_stop_refuses(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.stop()
    answer = intake.admit(_request())
    assert answer.refusal is ReviewRefusal.STOPPING
    assert calls == []
    assert not (tmp_path / "reviews").exists()


def test_an_unreviewed_repository_refuses(tmp_path: Path) -> None:
    _refused(tmp_path, _settings(), _request(repository="other/repo"),
             ReviewRefusal.REPOSITORY_NOT_REVIEWED)


def test_a_fork_on_an_unreviewed_repository_is_not_reviewed(
        tmp_path: Path) -> None:
    _refused(tmp_path, _settings(),
             _request(repository="other/repo", head_repository="other/fork"),
             ReviewRefusal.REPOSITORY_NOT_REVIEWED)


def test_a_head_from_another_repository_refuses(tmp_path: Path) -> None:
    _refused(tmp_path, _settings(), _request(head_repository="other/fork"),
             ReviewRefusal.HEAD_FROM_ANOTHER_REPOSITORY)


def test_a_head_from_elsewhere_beats_incomplete_roles(tmp_path: Path) -> None:
    _refused(tmp_path, _settings(team=_team(roles=False)),
             _request(head_repository="other/fork"),
             ReviewRefusal.HEAD_FROM_ANOTHER_REPOSITORY)


def test_incomplete_roles_or_musafir_refuses(tmp_path: Path) -> None:
    _refused(tmp_path, _settings(team=_team(roles=False)), _request(),
             ReviewRefusal.ROLES_INCOMPLETE)
    _refused(tmp_path, _settings(team=_team(musafir=False)), _request(),
             ReviewRefusal.ROLES_INCOMPLETE)


def test_an_accepted_review_is_handed_on(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    answer = intake.admit(_request())
    assert answer == ReviewAnswer(refusal=None)
    assert answer.accepted is True
    assert len(calls) == 1
    assert calls[0].request == _request()
    assert calls[0].team == _team()
    assert calls[0].checkout == Path("/srv/checkouts/name")


def test_a_head_repository_differing_only_in_case_is_accepted(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    answer = intake.admit(_request(head_repository="Owner/Name"))
    assert answer.accepted is True
    assert len(calls) == 1


def test_a_repository_spelled_in_another_case_is_found(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(key="Owner/Name"), tmp_path, calls.append)
    answer = intake.admit(_request())
    assert answer.accepted is True
    assert len(calls) == 1


def test_a_second_request_for_one_commit_raises_nothing_again(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    assert intake.admit(_request()).accepted is True
    assert intake.admit(_request()).accepted is True
    assert len(calls) == 1


def test_another_commit_and_another_number_are_raised_again(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    intake.admit(_request(head_commit=OTHER_COMMIT))
    intake.admit(_request(number=2))
    assert len(calls) == 3
    files = sorted(path.name for path in
                   (tmp_path / "reviews" / "owner" / "name").rglob("*.json"))
    assert files == [f"{OTHER_COMMIT}.json", f"{COMMIT}.json",
                     f"{COMMIT}.json"]


def test_the_same_commit_under_another_case_is_the_same_review(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    assert intake.admit(_request(repository="Owner/Name")).accepted is True
    assert intake.admit(_request(repository="owner/name")).accepted is True
    assert len(calls) == 1
    assert _record_file(tmp_path).is_file()


def test_a_corrupt_record_still_claims_the_commit(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    _record_file(tmp_path).write_text("{corrupt")
    assert intake.record_of(_request()) is None
    assert intake.admit(_request()).accepted is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    ['{"state": "accepted"}',
     '{"state": "bogus", "roles": {}, "musafir": null}',
     "corrupt",
     "[]",
     '{"state": "accepted", "roles": {}, "musafir": null, "extra": 1}',
     '{"state": 5, "roles": {}, "musafir": null}',
     '{"state": "accepted", "roles": [], "musafir": null}',
     '{"state": "accepted", "roles": {"r": 5}, "musafir": null}',
     '{"state": "accepted", "roles": {}, "musafir": 5}',
     '{"team": "architects", "state": "accepted", "roles": {}, '
     '"musafir": null}',
     '{"state": "accepted", "roles": {}, "musafir": null, '
     '"composer": "session-1"}',
     "[" * 100000 + "]" * 100000],
)
def test_a_record_that_is_not_valid_reads_none(tmp_path: Path,
                                               payload: str) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    _record_file(tmp_path).write_text(payload)
    assert intake.record_of(_request()) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_a_record_that_cannot_be_read_is_none(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    _record_file(tmp_path).chmod(0o000)
    assert intake.record_of(_request()) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters every directory")
def test_a_directory_above_that_cannot_be_entered_is_none(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    name = tmp_path / "reviews" / "owner" / "name"
    try:
        name.chmod(0o000)
        assert intake.record_of(_request()) is None
    finally:
        name.chmod(0o700)


@pytest.mark.parametrize("mask", [0o077, 0o777])
def test_modes_hold_whatever_the_umask(tmp_path: Path, mask: int) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    previous = os.umask(mask)
    try:
        intake.admit(_request())
    finally:
        os.umask(previous)
    reviews = tmp_path / "reviews"
    for directory in (reviews, reviews / "owner", reviews / "owner" / "name",
                      reviews / "owner" / "name" / "1"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(_record_file(tmp_path).stat().st_mode) == 0o600


def test_a_failed_update_raises_and_leaves_no_temporary(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    record = _record_file(tmp_path)
    record.unlink()
    record.mkdir()
    with pytest.raises(OSError):
        intake.update(_request(),
                      ReviewRecord(state=ReviewState.FINISHED))
    assert sorted(entry.name for entry in record.parent.iterdir()) == [
        f"{COMMIT}.json"]
    assert record.is_dir()


def test_an_update_for_another_number_replaces_only_it(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request(number=1))
    intake.admit(_request(number=2))
    replacement = ReviewRecord(state=ReviewState.FINISHED)
    intake.update(_request(number=2), replacement)
    assert intake.record_of(_request(number=2)) == replacement
    assert intake.record_of(_request(number=1)) == ReviewRecord(
        state=ReviewState.ACCEPTED)


def test_record_of_after_admit_and_update(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    assert intake.record_of(_request()) == ReviewRecord(
        state=ReviewState.ACCEPTED)
    replacement = ReviewRecord(state=ReviewState.RUNNING,
                               roles={"architect": "session-1"},
                               musafir="session-2")
    intake.update(_request(), replacement)
    assert intake.record_of(_request()) == replacement
    directory = _record_file(tmp_path).parent
    assert sorted(path.name for path in directory.iterdir()) == [
        f"{COMMIT}.json"]


def test_directories_are_private_and_files_owner_only(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    reviews = tmp_path / "reviews"
    for directory in (reviews, reviews / "owner", reviews / "owner" / "name",
                      reviews / "owner" / "name" / "1"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(_record_file(tmp_path).stat().st_mode) == 0o600


def test_after_stop_a_new_commit_leaves_no_record(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    intake.admit(_request())
    intake.stop()
    answer = intake.admit(_request(head_commit=OTHER_COMMIT))
    assert answer.refusal is ReviewRefusal.STOPPING
    assert len(calls) == 1
    assert not _record_file(tmp_path, OTHER_COMMIT).exists()
    assert _record_file(tmp_path).is_file()


def test_a_raising_on_accepted_propagates_after_the_record(
        tmp_path: Path) -> None:
    def boom(review: AcceptedReview) -> None:
        raise ValueError("the scheduler is gone")

    intake = ReviewIntake(_settings(), tmp_path, boom)
    with pytest.raises(ValueError):
        intake.admit(_request())
    assert intake.record_of(_request()) == ReviewRecord(
        state=ReviewState.ACCEPTED)
    calls: list[AcceptedReview] = []
    replay = ReviewIntake(_settings(), tmp_path, calls.append)
    assert replay.admit(_request()).accepted is True
    assert calls == []


def test_stopping_answers_before_each_other_refusal(tmp_path: Path) -> None:
    cases = [
        (_settings(), _request(repository="other/repo")),
        (_settings(), _request(head_repository="other/fork")),
        (_settings(team=_team(roles=False)), _request()),
    ]
    for index, (settings, asked) in enumerate(cases):
        calls: list[AcceptedReview] = []
        intake = ReviewIntake(settings, tmp_path / f"case{index}",
                              calls.append)
        intake.stop()
        answer = intake.admit(asked)
        assert answer.refusal is ReviewRefusal.STOPPING
        assert calls == []
        assert not (tmp_path / f"case{index}" / "reviews").exists()
    calls = []
    intake = ReviewIntake(_settings(), tmp_path / "recorded", calls.append)
    intake.admit(_request())
    intake.stop()
    assert intake.admit(_request()).refusal is ReviewRefusal.STOPPING
    assert len(calls) == 1
    assert (_record_file(tmp_path / "recorded")).is_file()


def test_the_second_repository_is_reviewed_with_its_own_team(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_two_settings(), tmp_path, calls.append)
    answer = intake.admit(_request(repository="owner/second",
                                   head_repository="owner/second"))
    assert answer.accepted is True
    assert len(calls) == 1
    assert calls[0].team.memory == Path("/srv/memory/second")
    assert calls[0].checkout == Path("/srv/checkouts/second")


def test_the_same_owner_another_name_is_not_reviewed(
        tmp_path: Path) -> None:
    _refused(tmp_path, _two_settings(), _request(repository="owner/other"),
             ReviewRefusal.REPOSITORY_NOT_REVIEWED)


def test_a_head_differing_only_in_its_name_refuses(tmp_path: Path) -> None:
    _refused(tmp_path, _two_settings(),
             _request(repository="owner/second",
                      head_repository="owner/first"),
             ReviewRefusal.HEAD_FROM_ANOTHER_REPOSITORY)


def test_directories_hold_the_casefolded_names(tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(key="Sakshi/Orchestrator"), tmp_path,
                          calls.append)
    intake.admit(_request(repository="Sakshi/Orchestrator",
                          head_repository="Sakshi/Orchestrator"))
    reviews = tmp_path / "reviews"
    assert sorted(entry.name for entry in reviews.iterdir()) == ["sakshi"]
    owner = reviews / "sakshi"
    assert sorted(entry.name for entry in owner.iterdir()) == [
        "orchestrator"]


def test_a_directory_at_the_record_path_counts_as_claimed(
        tmp_path: Path) -> None:
    calls: list[AcceptedReview] = []
    intake = ReviewIntake(_settings(), tmp_path, calls.append)
    number = tmp_path / "reviews" / "owner" / "name" / "1"
    number.mkdir(parents=True)
    (number / f"{COMMIT}.json").mkdir()
    answer = intake.admit(_request())
    assert answer.accepted is True
    assert calls == []

"""A review's run against a fake runtime: the roles' start arguments,
the record written before they start, one deadline over all of them and
the line a role that did not report leaves."""

from __future__ import annotations

import asyncio
import errno
import inspect
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple
from pathlib import Path

import pytest

import time

from orchestrator import review_run
from orchestrator.heads import HeadTurn, HeadTurnError, HeadTurnFailure
from orchestrator.review_intake import (AcceptedReview, ReviewIntake,
                                        ReviewState)
from orchestrator.review_run import ReviewRun, collect_reports
from orchestrator.review_workspace import (ReviewWorkspaceError,
                                           ReviewWorkspaces, Workspace)
from orchestrator.reviews import ReviewFailureReason, ReviewRequest
from orchestrator.sakshi_settings import (ReviewedRepository, ReviewTeam,
                                          RoleSettings, SakshiSettings)


REQUEST = ReviewRequest(repository="owner/name", number=1,
                        head_commit="a" * 40,
                        head_repository="owner/name")
CHECKOUT = Path("/srv/checkouts/name")
MEMORY = Path("/srv/memory/name")


def _role(head: str, seed: Path) -> RoleSettings:
    return RoleSettings(seed=seed, user=f"review_{head}", model="claude-3",
                        effort="high", head=head)


def _team(roles: dict[str, RoleSettings], deadline: float) -> ReviewTeam:
    return ReviewTeam(memory=MEMORY, deadline_seconds=deadline, roles=roles)


def _intake(tmp_path: Path, team: ReviewTeam) -> ReviewIntake:
    settings = SakshiSettings(
        workspace_root=Path("/srv/sakshi"),
        repositories={"owner/name": ReviewedRepository(
            checkout=CHECKOUT, team=team)})
    return ReviewIntake(settings, tmp_path, lambda review: None)


def _review(team: ReviewTeam) -> AcceptedReview:
    return AcceptedReview(request=REQUEST, team=team, checkout=CHECKOUT)


def _workspace(tmp_path: Path) -> Workspace:
    tree = tmp_path / "tree"
    tree.mkdir()
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    return Workspace(directory=tmp_path, tree=tree, reviews=reviews,
                     checkout=CHECKOUT)


class FakeRuntime:
    """Records every start's arguments and runs each role's script from
    the test; a role absent from the script answers a plain turn."""

    def __init__(self, script: Mapping[str, Callable[[], object]]):
        self.starts: list[dict] = []
        self.cancelled: list[str] = []
        self.entered: list[str] = []
        self.active = 0
        self.peak = 0
        self._script = script

    async def start(self, *, session_id: str, cwd: Path, prompt: str,
                    model: str, effort: str, add_dirs: Sequence[Path],
                    user: str) -> HeadTurn:
        name = cwd.name
        self.entered.append(name)
        self.current_add_dirs = list(add_dirs)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            action = self._script.get(name)
            if action is not None:
                # a script may take the start's add_dirs, or nothing
                arity = len(inspect.signature(action).parameters)
                result = action(list(add_dirs)) if arity else action()
                if asyncio.iscoroutine(result):
                    try:
                        result = await result
                    except asyncio.CancelledError:
                        self.cancelled.append(name)
                        raise
                turn = HeadTurn(text=str(result), session_id=session_id)
            else:
                turn = HeadTurn(text=f"report of {name}",
                                session_id=session_id)
            self.starts.append({
                "session_id": session_id, "cwd": cwd, "prompt": prompt,
                "model": model, "effort": effort,
                "add_dirs": list(add_dirs), "user": user,
            })
            return turn
        finally:
            self.active -= 1

    async def turn(self, *, session_id: str, cwd: Path,
                   prompt: str) -> HeadTurn:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _record_snapshot(intake: ReviewIntake) -> object:
    return intake.record_of(REQUEST)


def test_every_role_starts_with_exact_arguments(
        tmp_path: Path) -> None:
    team = _team({"architect": _role("01", Path("/srv/seeds/architect")),
                  "razor": _role("02", Path("/srv/seeds/razor"))},
                 deadline=30.0)
    intake = _intake(tmp_path, team)
    runtime = FakeRuntime({})
    workspace = _workspace(tmp_path)

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, intake, _review(team),
                                     workspace)

    asyncio.run(scenario())
    starts = {start["cwd"].name: start for start in runtime.starts}
    assert set(starts) == {"architect", "razor"}
    for name, role in team.roles.items():
        start = starts[name]
        assert start["model"] == role.model
        assert start["effort"] == role.effort
        assert start["user"] == role.user
        assert start["add_dirs"] == [workspace.tree, MEMORY / role.head]
        assert start["prompt"] == "\n".join([
            "Review this pull request.",
            f"Repository: owner/name",
            f"Number: 1",
            f"Head commit: {'a' * 40}",
            "URL: https://github.com/owner/name/pull/1",
            f"Checkout: {tmp_path / 'tree'}",
            f"Memory: {MEMORY / role.head}",
        ])
        assert start["session_id"] != ""


def test_the_record_is_running_with_all_ids_before_any_role_starts(
        tmp_path: Path) -> None:
    team = _team({"architect": _role("01", Path("/srv/seeds/architect")),
                  "razor": _role("02", Path("/srv/seeds/razor"))},
                 deadline=30.0)
    intake = _intake(tmp_path, team)
    seen: list[object] = []

    async def script() -> object:
        seen.append(_record_snapshot(intake))
        return "report"

    runtime = FakeRuntime({"architect": script, "razor": script})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, intake, _review(team),
                                     _workspace(tmp_path))

    reports = asyncio.run(scenario())
    assert set(reports) == {"architect", "razor"}
    assert len(seen) == 2
    for snapshot in seen:
        assert snapshot is not None
        assert snapshot.state is ReviewState.RUNNING
        assert set(snapshot.roles) == {"architect", "razor"}
    ids = [snapshot.roles for snapshot in seen]
    assert ids[0] == ids[1]
    assert len(set(ids[0].values())) == 2


def test_the_roles_run_together(tmp_path: Path) -> None:
    team = _team({"one": _role("01", Path("/srv/seeds/one")),
                  "two": _role("02", Path("/srv/seeds/two")),
                  "three": _role("03", Path("/srv/seeds/three"))},
                 deadline=30.0)

    async def slow_report() -> str:
        await asyncio.sleep(0.2)
        return "report"

    runtime = FakeRuntime({"one": slow_report, "two": slow_report,
                           "three": slow_report})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    asyncio.run(scenario())
    assert runtime.peak == 3


def test_a_deadline_cancels_the_laggard_and_the_others_report(
        tmp_path: Path) -> None:
    team = _team({"fast": _role("01", Path("/srv/seeds/fast")),
                  "slow": _role("02", Path("/srv/seeds/slow"))},
                 deadline=0.3)

    async def laggard() -> str:
        await asyncio.sleep(30)
        return "late"

    runtime = FakeRuntime({"slow": laggard})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    reports = asyncio.run(scenario())
    assert reports == {"fast": "report of fast"}
    assert runtime.cancelled == ["slow"]


@pytest.mark.parametrize("failure", [
    HeadTurnError(HeadTurnFailure.TURN_FAILED),
    HeadTurnError(HeadTurnFailure.RATE_LIMITED),
    HeadTurnError(HeadTurnFailure.OUTCOME_UNKNOWN),
    ValueError("boom"),
])
def test_a_role_that_does_not_report_leaves_one_line(
        tmp_path: Path, failure: BaseException, capsys) -> None:
    team = _team({"architect": _role("01", Path("/srv/seeds/architect")),
                  "broken": _role("02", Path("/srv/seeds/broken"))},
                 deadline=30.0)

    def explode() -> object:
        raise failure

    runtime = FakeRuntime({"broken": explode})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    reports = asyncio.run(scenario())
    assert reports == {"architect": "report of architect"}
    line = capsys.readouterr().out.splitlines()
    expected = (failure.failure.value if isinstance(failure, HeadTurnError)
                else type(failure).__name__)
    assert line == [f"Review owner/name#1: role broken did not report: "
                    f"{expected}"]


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_an_empty_or_whitespace_text_is_not_a_report(
        tmp_path: Path, text: str) -> None:
    team = _team({"silent": _role("01", Path("/srv/seeds/silent"))},
                 deadline=30.0)
    runtime = FakeRuntime({"silent": lambda: text})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    assert asyncio.run(scenario()) == {}


def test_no_report_returns_nothing(tmp_path: Path, capsys) -> None:
    team = _team({"one": _role("01", Path("/srv/seeds/one")),
                  "two": _role("02", Path("/srv/seeds/two"))},
                 deadline=30.0)

    def explode() -> object:
        raise HeadTurnError(HeadTurnFailure.TURN_FAILED)

    runtime = FakeRuntime({"one": explode, "two": explode})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    assert asyncio.run(scenario()) == {}
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_an_async_script_returning_blank_text_yields_no_report(
        tmp_path: Path) -> None:
    team = _team({"silent": _role("01", Path("/srv/seeds/silent"))},
                 deadline=30.0)

    async def script() -> str:
        return ""

    runtime = FakeRuntime({"silent": script})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    assert asyncio.run(scenario()) == {}


def test_an_async_script_returning_text_yields_that_text(
        tmp_path: Path) -> None:
    team = _team({"speaker": _role("01", Path("/srv/seeds/speaker"))},
                 deadline=30.0)

    async def script() -> str:
        return "hello"

    runtime = FakeRuntime({"speaker": script})

    async def scenario() -> dict[str, str]:
        return await collect_reports(runtime, _intake(tmp_path, team),
                                     _review(team), _workspace(tmp_path))

    assert asyncio.run(scenario()) == {"speaker": "hello"}

# ---------------------------------------------------------------------------
# The run: the musafir over the reports and the review's end
# ---------------------------------------------------------------------------


class _Setup(NamedTuple):
    runner: ReviewRun
    runtime: FakeRuntime
    review: AcceptedReview
    reported: list
    root: Path
    intake: ReviewIntake
    workspaces: ReviewWorkspaces


def _stand_request(git_stand: GitStand) -> ReviewRequest:
    return ReviewRequest(repository="owner/name", number=1,
                         head_commit=git_stand.head_commit,
                         head_repository="owner/name")


def _full_team(deadline: float = 30.0,
               roles: dict[str, RoleSettings] | None = None,
               ) -> ReviewTeam:
    return ReviewTeam(
        memory=MEMORY, deadline_seconds=deadline,
        roles=roles or {"reviewer-1": _role("01",
                                            Path("/srv/seeds/reviewer-1"))},
        musafir=RoleSettings(seed=Path("/srv/seeds/musafir"),
                             user="review_musafir", model="claude-3",
                             effort="medium", head="04"))


def _runner(tmp_path: Path, git_stand: GitStand, team: ReviewTeam,
            script: Mapping[str, object], *, root: Path | None = None,
            report=None) -> _Setup:
    runtime = FakeRuntime(script)
    intake = _intake(tmp_path, team)
    root_path = tmp_path / "workspaces" if root is None else root
    if root is None:
        root_path.mkdir()
    reported: list = []
    workspaces = ReviewWorkspaces(root_path)
    runner = ReviewRun(runtime=runtime, intake=intake,
                       workspaces=workspaces,
                       report_failure=report or (
                           lambda request, reason: reported.append(
                               (request, reason))))
    review = AcceptedReview(request=_stand_request(git_stand),
                            team=team, checkout=git_stand.checkout)
    return _Setup(runner, runtime, review, reported, root_path, intake,
                  workspaces)


def test_the_musafir_starts_with_exact_arguments_and_prompt(
        tmp_path: Path, git_stand: GitStand) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(), {})

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    asyncio.run(scenario())
    musafir = [start for start in setup.runtime.starts
               if start["cwd"].name == "musafir"]
    assert len(musafir) == 1
    start = musafir[0]
    assert start["model"] == "claude-3"
    assert start["effort"] == "medium"
    assert start["user"] == "review_musafir"
    reviews = start["add_dirs"][0]
    assert start["add_dirs"] == [reviews, MEMORY / "04"]
    assert start["prompt"] == "\n".join([
        "Compose the reviews of this pull request.",
        "Repository: owner/name",
        "Number: 1",
        f"Head commit: {git_stand.head_commit}",
        "URL: https://github.com/owner/name/pull/1",
        f"Reviews: {reviews}",
        f"Memory: {MEMORY / '04'}",
    ])
    assert setup.reported == []
    assert list(setup.root.iterdir()) == []


def test_reports_land_only_after_the_roles_and_before_the_musafir(
        tmp_path: Path, git_stand: GitStand) -> None:
    seen: dict[str, list[str]] = {}

    def role_looks(add_dirs) -> str:
        reviews = add_dirs[0].parent / "reviews"
        seen["role"] = sorted(entry.name for entry in reviews.iterdir())
        return "a report"

    def musafir_looks(add_dirs) -> str:
        seen["musafir"] = sorted(entry.name
                                 for entry in add_dirs[0].iterdir())
        return "composed"

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": role_looks, "musafir": musafir_looks})

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    asyncio.run(scenario())
    assert seen["role"] == []
    assert seen["musafir"] == ["reviewer-1.md"]


def test_the_states_run_running_composing_finished(
        tmp_path: Path, git_stand: GitStand) -> None:
    seen: dict[str, str] = {}
    request_holder: dict = {}

    def read_state() -> str:
        record = request_holder["intake"].record_of(
            request_holder["request"])
        return record.state.value if record else "none"

    def role_script(add_dirs) -> str:
        seen["role"] = read_state()
        return "a report"

    def musafir_script(add_dirs) -> str:
        seen["musafir"] = read_state()
        return "composed"

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": role_script, "musafir": musafir_script})
    request_holder["intake"] = setup.intake
    request_holder["request"] = setup.review.request

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    asyncio.run(scenario())
    final = setup.intake.record_of(setup.review.request)
    assert seen["role"] == "running"
    assert seen["musafir"] == "composing"
    assert final.state.value == "finished"
    assert final.musafir is not None
    assert list(setup.root.iterdir()) == []


def test_no_report_means_no_musafir_and_one_no_role_report(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    def explode(add_dirs) -> object:
        raise HeadTurnError(HeadTurnFailure.TURN_FAILED)

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": explode})

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    asyncio.run(scenario())
    assert "musafir" not in setup.runtime.entered
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.NO_ROLE_REPORTED
    record = setup.intake.record_of(setup.review.request)
    assert record.state.value == "failed"
    assert list(setup.root.iterdir()) == []


@pytest.mark.parametrize("kind", ["error", "deadline"])
def test_a_musafir_that_gives_no_output_fails_the_review(
        tmp_path: Path, git_stand: GitStand, kind: str) -> None:
    async def laggard(add_dirs) -> str:
        await asyncio.sleep(30)
        return "late"

    def explode(add_dirs) -> object:
        raise HeadTurnError(HeadTurnFailure.RATE_LIMITED)

    team = _full_team(deadline=0.3 if kind == "deadline" else 30.0)
    setup = _runner(tmp_path, git_stand, team,
                    {"musafir": laggard if kind == "deadline" else explode})

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    asyncio.run(scenario())
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.MUSAFIR_GAVE_NO_OUTPUT
    if kind == "deadline":
        assert setup.runtime.cancelled == ["musafir"]
    record = setup.intake.record_of(setup.review.request)
    assert record.state.value == "failed"
    assert record.musafir is not None
    assert list(setup.root.iterdir()) == []


def test_a_workspace_error_is_workspace_not_laid(
        tmp_path: Path, git_stand: GitStand) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(), {},
                    root=tmp_path / "no-such-root")

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    asyncio.run(scenario())
    assert setup.runtime.entered == []
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.WORKSPACE_NOT_LAID
    record = setup.intake.record_of(setup.review.request)
    assert record.state.value == "failed"


@pytest.mark.parametrize("stage", ["roles", "musafir"])
def test_a_cancel_cuts_the_review_and_is_raised_again(
        tmp_path: Path, git_stand: GitStand, stage: str) -> None:
    async def hold(add_dirs) -> str:
        await asyncio.sleep(30)
        return "late"

    script = ({"reviewer-1": hold} if stage == "roles"
              else {"musafir": hold})
    setup = _runner(tmp_path, git_stand, _full_team(deadline=60.0), script)
    waited = "reviewer-1" if stage == "roles" else "musafir"

    async def scenario() -> None:
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        deadline = time.monotonic() + 5.0
        while (waited not in setup.runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.02)
        assert waited in setup.runtime.entered
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.STOP_CUT_THE_REVIEW
    record = setup.intake.record_of(setup.review.request)
    assert record.state.value == "failed"
    assert list(setup.root.iterdir()) == []


def test_the_failure_report_does_not_block_the_loop(
        tmp_path: Path, git_stand: GitStand) -> None:
    def explode(add_dirs) -> object:
        raise HeadTurnError(HeadTurnFailure.TURN_FAILED)

    entered = threading.Event()
    ticked = threading.Event()
    waits: list[bool] = []

    def blocking_report(request, reason) -> None:
        entered.set()
        waits.append(ticked.wait(5.0))

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": explode}, report=blocking_report)

    async def ticker() -> None:
        deadline = time.monotonic() + 5.0
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        # an expired entry deadline fails here and never releases the
        # reporter
        assert entered.is_set()
        ticked.set()

    async def scenario() -> None:
        await asyncio.gather(setup.runner.run(setup.review), ticker())

    asyncio.run(scenario())
    # a reporter that runs on the loop never lets the ticker run, so
    # its wait ends at the bound with ticked unset
    assert entered.is_set()
    assert waits == [True]


def test_a_failed_review_without_an_error_prints_its_reason(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": lambda add_dirs: "   "})

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: no-role-reported"]


def test_a_failed_review_with_an_error_prints_reason_and_type(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(), {},
                    root=tmp_path / "no-such-root")

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: workspace-not-laid: "
        "ReviewWorkspaceError"]


def test_a_raising_report_never_leaves_a_failed_run(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    def explode(request, reason) -> None:
        raise RuntimeError("no credential")

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": lambda add_dirs: ""},
                    report=explode)

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert len(setup.reported) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: no-role-reported",
        "Review owner/name#1: failure report not sent: RuntimeError"]


def test_a_raising_report_never_replaces_a_cancel(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    async def hold(add_dirs) -> str:
        await asyncio.sleep(30)
        return "late"

    def explode(request, reason) -> None:
        raise RuntimeError("no credential")

    setup = _runner(tmp_path, git_stand, _full_team(deadline=60.0),
                    {"reviewer-1": hold}, report=explode)

    async def scenario() -> None:
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        while "reviewer-1" not in setup.runtime.entered:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    capsys.readouterr()
    asyncio.run(scenario())
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "Review owner/name#1 failed: stop-cut-the-review",
        "Review owner/name#1: failure report not sent: RuntimeError"]


def test_a_cancel_during_the_report_waits_for_it_and_reports_once(
        tmp_path: Path, git_stand: GitStand) -> None:
    release = threading.Event()
    finished: list[bool] = []
    held: list[bool] = []
    calls: list = []

    def blocking_report(request, reason) -> None:
        calls.append((request, reason))
        release.wait(5.0)
        finished.append(True)

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": lambda add_dirs: ""},
                    report=blocking_report)

    async def scenario() -> None:
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        while len(calls) < 1:
            await asyncio.sleep(0.02)
        try:
            task.cancel()
            # the run has not ended while the report holds
            await asyncio.sleep(0.1)
            held.append(task.done())
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][1] is ReviewFailureReason.NO_ROLE_REPORTED
    assert held == [False]
    assert finished == [True]


def test_a_cancel_mid_roles_ends_the_roles_and_none_is_left_running(
        tmp_path: Path, git_stand: GitStand) -> None:
    async def hold(add_dirs) -> str:
        await asyncio.sleep(30)
        return "late"

    setup = _runner(tmp_path, git_stand, _full_team(deadline=60.0),
                    {"reviewer-1": hold})
    seen: dict[str, object] = {}

    async def scenario() -> None:
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        while "reviewer-1" not in setup.runtime.entered:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # read inside the loop: loop shutdown would cancel the role
        # anyway, and only the run's own ending counts here
        seen["cancelled"] = list(setup.runtime.cancelled)
        seen["active"] = setup.runtime.active

    asyncio.run(scenario())
    assert seen["cancelled"] == ["reviewer-1"]
    assert seen["active"] == 0
    assert list(setup.root.iterdir()) == []


# ---------------------------------------------------------------------------
# Sakshi's own storage failing, and the removal's own end
# ---------------------------------------------------------------------------


def test_a_failed_write_of_the_reports_is_storage_failed(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": lambda add_dirs: "a report"})

    def failing_write(workspace: Workspace,
                      reports: Mapping[str, str]) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    setup.workspaces.write_reviews = failing_write

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert "musafir" not in setup.runtime.entered
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.STORAGE_FAILED
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: storage-failed: OSError"]
    record = setup.intake.record_of(setup.review.request)
    assert record.state.value == "failed"
    assert list(setup.root.iterdir()) == []


def test_a_first_record_write_that_fails_is_storage_failed(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(), {})
    updates: list[int] = []
    real_update = setup.intake.update

    def failing_update(request: ReviewRequest, record) -> None:
        updates.append(1)
        if len(updates) == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_update(request, record)

    setup.intake.update = failing_update

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert setup.runtime.entered == []
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.STORAGE_FAILED
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: storage-failed: OSError"]
    record = setup.intake.record_of(setup.review.request)
    assert record.state.value == "failed"
    assert list(setup.root.iterdir()) == []


def test_an_async_script_returning_blank_text_fails_as_no_role_reported(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    async def script(add_dirs) -> str:
        return ""

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": script})

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert len(setup.reported) == 1
    assert setup.reported[0][1] is ReviewFailureReason.NO_ROLE_REPORTED
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: no-role-reported"]


def _no_worktree_registration(checkout: Path) -> bool:
    worktrees = checkout / ".git" / "worktrees"
    return not worktrees.exists() or not any(worktrees.iterdir())


def test_a_cancel_while_the_removal_waits_for_the_lock_still_removes(
        tmp_path: Path, git_stand: GitStand) -> None:
    async def scenario() -> _Setup:
        release = asyncio.Event()

        async def sleeper(add_dirs) -> str:
            await asyncio.wait_for(release.wait(), 5.0)
            return "composed"

        setup = _runner(tmp_path, git_stand, _full_team(),
                        {"musafir": sleeper})
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        deadline = time.monotonic() + 5.0
        while ("musafir" not in setup.runtime.entered
               and time.monotonic() < deadline):
            await asyncio.sleep(0.02)
        assert "musafir" in setup.runtime.entered
        lock = setup.workspaces._lock_for(git_stand.checkout)
        await lock.acquire()
        try:
            # the run reaches the lock only once the test holds it
            release.set()
            finish_by = time.monotonic() + 5.0
            while (setup.intake.record_of(
                    setup.review.request).state
                    is not ReviewState.FINISHED
                    and time.monotonic() < finish_by):
                await asyncio.sleep(0.02)
            record = setup.intake.record_of(setup.review.request)
            assert record.state is ReviewState.FINISHED
            task.cancel()
        finally:
            lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        return setup

    setup = asyncio.run(scenario())
    assert list(setup.root.iterdir()) == []
    assert _no_worktree_registration(git_stand.checkout)


def test_a_cancel_during_the_removals_worktree_list_still_removes(
        tmp_path: Path, git_stand: GitStand) -> None:
    listing: list[float] = []

    async def scenario() -> _Setup:
        setup = _runner(tmp_path, git_stand, _full_team(), {})
        release = asyncio.Event()
        real_git = setup.workspaces._git

        async def slowed_git(arguments, cwd: Path) -> str:
            if arguments[:2] == ["worktree", "list"]:
                listing.append(time.monotonic())
                await asyncio.wait_for(release.wait(), 5.0)
            return await real_git(arguments, cwd)

        setup.workspaces._git = slowed_git
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        deadline = time.monotonic() + 5.0
        try:
            while (not listing and time.monotonic() < deadline):
                await asyncio.sleep(0.02)
            assert listing
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return setup

    setup = asyncio.run(scenario())
    assert list(setup.root.iterdir()) == []
    assert _no_worktree_registration(git_stand.checkout)


def test_a_removal_that_raises_leaves_the_run_returning_normally(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    setup = _runner(tmp_path, git_stand, _full_team(), {})

    async def failing_remove(workspace: Workspace) -> None:
        raise ReviewWorkspaceError("worktree remove failed: forced")

    setup.workspaces.remove = failing_remove

    async def scenario() -> None:
        await setup.runner.run(setup.review)

    capsys.readouterr()
    asyncio.run(scenario())
    assert setup.reported == []
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1: workspace removal failed: "
        "ReviewWorkspaceError"]


def test_a_cancel_during_a_raising_removal_is_raised_after_it(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    entered: list[bool] = []
    held: list[bool] = []

    async def scenario() -> _Setup:
        setup = _runner(tmp_path, git_stand, _full_team(), {})
        release = asyncio.Event()

        async def failing_remove(workspace: Workspace) -> None:
            entered.append(True)
            await asyncio.wait_for(release.wait(), 5.0)
            raise ReviewWorkspaceError("worktree remove failed: forced")

        setup.workspaces.remove = failing_remove
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        deadline = time.monotonic() + 5.0
        try:
            while (not entered and time.monotonic() < deadline):
                await asyncio.sleep(0.02)
            assert entered
            task.cancel()
            # the run has not ended while the removal holds
            await asyncio.sleep(0.1)
            held.append(task.done())
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return setup

    capsys.readouterr()
    setup = asyncio.run(scenario())
    assert setup.reported == []
    assert held == [False]
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1: workspace removal failed: "
        "ReviewWorkspaceError"]


def test_a_cancel_while_a_raising_report_runs_is_raised_after_it(
        tmp_path: Path, git_stand: GitStand, capsys) -> None:
    entered = threading.Event()
    release = threading.Event()
    held: list[bool] = []
    calls: list = []

    def raising_report(request, reason) -> None:
        calls.append((request, reason))
        entered.set()
        release.wait(5.0)
        raise RuntimeError("no credential")

    setup = _runner(tmp_path, git_stand, _full_team(),
                    {"reviewer-1": lambda add_dirs: ""},
                    report=raising_report)

    async def scenario() -> None:
        task = asyncio.ensure_future(setup.runner.run(setup.review))
        deadline = time.monotonic() + 5.0
        try:
            while (not entered.is_set() and time.monotonic() < deadline):
                await asyncio.sleep(0.02)
            assert entered.is_set()
            task.cancel()
            # the run has not ended while the report holds
            await asyncio.sleep(0.1)
            held.append(task.done())
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    capsys.readouterr()
    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][1] is ReviewFailureReason.NO_ROLE_REPORTED
    assert held == [False]
    assert capsys.readouterr().out.splitlines() == [
        "Review owner/name#1 failed: no-role-reported",
        "Review owner/name#1: failure report not sent: RuntimeError"]

"""A review's run: its roles started together under one deadline, the
record of their sessions written before the first of them starts, the
musafir raised over their reports and the review ended.

The engine hands each role its prompt — the contract with the seeds —
and nothing of the pull request but the validated fields travels in
it. One role's end never affects another and nothing is retried; the
runtime is reached only through its protocol and the failure report
only through a callable, so a fake runtime and a fake report test this
module.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping

from .heads import HeadRuntimeProtocol, HeadTurn, HeadTurnError
from .review_intake import (AcceptedReview, ReviewIntake, ReviewRecord,
                            ReviewState)
from .review_workspace import ReviewWorkspaces, Workspace
from .reviews import ReviewFailureReason, ReviewRequest
from .sakshi_settings import ReviewTeam, RoleSettings
from .waiting import wait_out


def _prompt(request: ReviewRequest, team: ReviewTeam,
            role: RoleSettings, workspace: Workspace) -> str:
    """The contract between the engine and the role seeds: the pull
    request's validated fields, the tree and the role's own memory
    folder, and nothing else."""
    memory = team.memory / role.head
    return "\n".join([
        "Review this pull request.",
        f"Repository: {request.repository}",
        f"Number: {request.number}",
        f"Head commit: {request.head_commit}",
        f"URL: https://github.com/{request.repository}/pull/{request.number}",
        f"Checkout: {workspace.tree}",
        f"Memory: {memory}",
    ])


def _did_not_report(request: ReviewRequest, name: str,
                    failure: BaseException) -> None:
    """Name a role that did not report on standard output: the failure
    by its meaning only, never a detail."""
    if isinstance(failure, HeadTurnError):
        reason = failure.failure.value
    else:
        reason = type(failure).__name__
    print(f"Review {request.repository}#{request.number}: "
          f"role {name} did not report: {reason}")


async def collect_reports(runtime: HeadRuntimeProtocol,
                          intake: ReviewIntake,
                          review: AcceptedReview,
                          workspace: Workspace) -> dict[str, str]:
    """Run one review's roles together under the team's deadline and
    return each reporting role's text.

    The record holds every role's session id from the running state on,
    written before any role starts, so a person can open a role's
    session even when the review later fails. The wait ends when every
    role has ended or the deadline has passed since they started; a
    role still running is cancelled and awaited, which ends its child.
    No file is written and nothing is removed.
    """
    request, team = review.request, review.team
    ids = {name: str(uuid.uuid4()) for name in team.roles}
    intake.update(request, ReviewRecord(state=ReviewState.RUNNING,
                                        roles=ids))
    tasks = {
        name: asyncio.ensure_future(runtime.start(
            session_id=session_id,
            cwd=role.seed,
            prompt=_prompt(request, team, role, workspace),
            model=role.model,
            effort=role.effort,
            add_dirs=[workspace.tree, team.memory / role.head],
            user=role.user,
        ))
        for name, (role, session_id) in _paired(team.roles, ids)
    }
    try:
        done, pending = await asyncio.wait(tasks.values(),
                                           timeout=team.deadline_seconds)
    except asyncio.CancelledError:
        # a stop mid-roles ends every role still running, so no start
        # and no child outlives the review and its removed workspace
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    reports: dict[str, str] = {}
    for name, task in tasks.items():
        if task.cancelled():
            # the deadline ended this role; its child ended with it
            _did_not_report(request, name, asyncio.CancelledError())
        elif task.exception() is not None:
            _did_not_report(request, name, task.exception())
        elif task.result().text.strip():
            reports[name] = task.result().text
    return reports


def _paired(roles: Mapping[str, RoleSettings],
            ids: Mapping[str, str]):
    """Each role with its session id, in the team's order."""
    for name, role in roles.items():
        yield name, (role, ids[name])


def _musafir_prompt(request: ReviewRequest, team: ReviewTeam,
                    musafir: RoleSettings, workspace: Workspace) -> str:
    """The contract with the musafir's seed: the pull request's
    validated fields, the reviews directory and its own memory
    folder."""
    memory = team.memory / musafir.head
    return "\n".join([
        "Compose the reviews of this pull request.",
        f"Repository: {request.repository}",
        f"Number: {request.number}",
        f"Head commit: {request.head_commit}",
        f"URL: https://github.com/{request.repository}/pull/{request.number}",
        f"Reviews: {workspace.reviews}",
        f"Memory: {memory}",
    ])


class ReviewRun:
    """One review's whole run: the workspace, the roles, the record, the
    musafir and the failure report, ending one way or another."""

    def __init__(self, *, runtime: HeadRuntimeProtocol,
                 intake: ReviewIntake,
                 workspaces: ReviewWorkspaces,
                 report_failure:
                 Callable[[ReviewRequest, ReviewFailureReason], None]
                 ) -> None:
        self._runtime = runtime
        self._intake = intake
        self._workspaces = workspaces
        self._report_failure = report_failure

    async def run(self, review: AcceptedReview) -> None:
        """Run one accepted review to its end.

        The musafir is raised only after a role reported, and its
        returned turn finishes the review whatever its text; a failed
        review is recorded, named on standard output and reported to
        the person off the loop. A cancellation is a failed review too,
        and is raised again. The workspace is removed on every way out,
        and a cancellation waits the removal out.
        """
        request, team = review.request, review.team
        workspace: Workspace | None = None
        musafir_id: str | None = None
        # once a failure's report has begun, a cancel that lands during
        # it is raised after the report and never reclassifies the
        # review: it is reported once, as the failure it had begun
        reported_failure = False
        try:
            try:
                workspace = await self._workspaces.prepare(
                    checkout=review.checkout, request=request)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                reported_failure = True
                await self._failed(request, {}, None,
                                   ReviewFailureReason.WORKSPACE_NOT_LAID,
                                   error)
                return
            try:
                reports = await collect_reports(self._runtime, self._intake,
                                                review, workspace)
                role_ids = self._ids(request)
                if not reports:
                    reported_failure = True
                    await self._failed(request, role_ids, None,
                                       ReviewFailureReason.NO_ROLE_REPORTED,
                                       None)
                    return
                self._workspaces.write_reviews(workspace, reports)
                musafir_id = str(uuid.uuid4())
                self._record(request, ReviewRecord(
                    state=ReviewState.COMPOSING, roles=role_ids,
                    musafir=musafir_id))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Sakshi's own storage failed: the first record write or
                # the reports' writing
                reported_failure = True
                await self._failed(request, self._ids(request),
                                   musafir_id,
                                   ReviewFailureReason.STORAGE_FAILED,
                                   error)
                return
            musafir = team.musafir
            if musafir is None:
                # the intake refuses a team without a musafir
                reported_failure = True
                await self._failed(request, role_ids, None,
                                   ReviewFailureReason.NO_ROLE_REPORTED,
                                   None)
                return
            turn = asyncio.ensure_future(self._runtime.start(
                session_id=musafir_id,
                cwd=musafir.seed,
                prompt=_musafir_prompt(request, team, musafir, workspace),
                model=musafir.model,
                effort=musafir.effort,
                add_dirs=[workspace.reviews, team.memory / musafir.head],
                user=musafir.user,
            ))
            try:
                await asyncio.wait_for(turn, team.deadline_seconds)
            except TimeoutError:
                # the deadline cancels the musafir, ending its child
                reported_failure = True
                await self._failed(request, role_ids, musafir_id,
                                   ReviewFailureReason.MUSAFIR_GAVE_NO_OUTPUT,
                                   None)
                return
            except asyncio.CancelledError:
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
                raise
            except Exception as error:
                reported_failure = True
                await self._failed(request, role_ids, musafir_id,
                                   ReviewFailureReason.MUSAFIR_GAVE_NO_OUTPUT,
                                   error)
                return
            self._record(request, ReviewRecord(
                state=ReviewState.FINISHED, roles=role_ids,
                musafir=musafir_id))
        except asyncio.CancelledError:
            if not reported_failure:
                await self._failed(request, self._ids(request),
                                   musafir_id,
                                   ReviewFailureReason.STOP_CUT_THE_REVIEW,
                                   None)
            raise
        finally:
            if workspace is not None:
                async def removing() -> None:
                    try:
                        await self._workspaces.remove(workspace)
                    except Exception as error:
                        # a failing cleanup never replaces how run ends
                        print(f"Review {request.repository}"
                              f"#{request.number}: "
                              f"workspace removal failed: "
                              f"{type(error).__name__}")
                # the cancellation does not interrupt the removal: it
                # runs as its own task and is only waited out
                await wait_out(asyncio.ensure_future(removing()))

    def _ids(self, request: ReviewRequest) -> dict[str, str]:
        """The session ids the record holds now, whatever they are."""
        try:
            record = self._intake.record_of(request)
        except Exception:
            return {}
        return dict(record.roles) if record else {}

    def _record(self, request: ReviewRequest, record: ReviewRecord) -> None:
        """Write a record step, guarded: a failing one prints a line."""
        try:
            self._intake.update(request, record)
        except Exception as error:
            print(f"Review {request.repository}#{request.number}: "
                  f"record not written: {type(error).__name__}")

    async def _failed(self, request: ReviewRequest,
                      roles: dict[str, str], musafir: str | None,
                      reason: ReviewFailureReason,
                      error: BaseException | None) -> None:
        """End a review as failed: the record with the ids it has, one
        line naming the reason — and the error's type when there is
        one — and the person's report off the loop."""
        self._record(request, ReviewRecord(
            state=ReviewState.FAILED, roles=roles, musafir=musafir))
        line = (f"Review {request.repository}#{request.number} failed: "
                f"{reason.value}")
        if error is not None:
            line += f": {type(error).__name__}"
        print(line)

        async def reporting() -> None:
            try:
                await asyncio.to_thread(self._report_failure, request,
                                        reason)
            except Exception as send_error:
                print(f"Review {request.repository}#{request.number}: "
                      f"failure report not sent: "
                      f"{type(send_error).__name__}")

        # a cancellation that lands while the report runs — a raising
        # report included — waits for the report's end and is raised
        # after it, so the review is reported once
        await wait_out(asyncio.ensure_future(reporting()))

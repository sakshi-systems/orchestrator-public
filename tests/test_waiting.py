"""The shared wait: a task waited out under a shield, the cancellation
that lands in the wait raised once the task has ended."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.waiting import wait_out


def test_the_wait_out_helper_raises_at_once_over_a_cancelled_task(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    real_shield = asyncio.shield

    def bounded_shield(inner):
        calls.append(inner)
        if len(calls) > 3:
            raise AssertionError("the wait spins on asyncio.shield")
        return real_shield(inner)

    monkeypatch.setattr(asyncio, "shield", bounded_shield)

    async def scenario() -> None:
        doomed = asyncio.ensure_future(asyncio.sleep(30))
        doomed.cancel()
        await asyncio.sleep(0)
        assert doomed.cancelled()
        with pytest.raises(asyncio.CancelledError):
            await wait_out(doomed)

    asyncio.run(scenario())
    assert 1 <= len(calls) <= 3


def test_the_wait_out_helper_raises_only_after_the_waited_task_ends() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        ended: list[bool] = []

        async def body() -> None:
            await release.wait()
            ended.append(True)

        task = asyncio.ensure_future(body())
        waiter = asyncio.ensure_future(wait_out(task))
        await asyncio.sleep(0.05)
        waiter.cancel()
        await asyncio.sleep(0.05)
        # the cancel is remembered and the wait goes on
        assert not waiter.done()
        assert ended == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert ended == [True]
        assert task.done() and not task.cancelled()

    asyncio.run(scenario())

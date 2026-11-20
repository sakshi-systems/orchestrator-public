"""One shared wait: a task waited out under a shield, the cancellation
that lands in the wait raised once the task has ended."""

from __future__ import annotations

import asyncio


async def wait_out(task: asyncio.Future) -> None:
    """Wait for a task under a shield until it has ended, raising the
    cancellation that landed in the wait once it has.

    A cancellation that lands in the wait is remembered and the wait
    goes on.  When the waited task is itself done or cancelled, a
    remembered or arriving cancellation is raised at once, so the wait
    never loops on a shield that raises without yielding.  An Exception
    the task ended with never leaves here: each task catches its own
    Exception and prints its own line, so a task that raised still lets
    the remembered cancellation be raised after it."""
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                raise
        except Exception:
            break
        else:
            break
    if cancelled:
        raise asyncio.CancelledError

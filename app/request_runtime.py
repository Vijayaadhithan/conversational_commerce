"""Bounded blocking storage and cooperative deadlines across async/thread work."""

import asyncio
import time
from contextvars import ContextVar

from fastapi import HTTPException

deadline = ContextVar("turn_deadline", default=None)
usage_context = ContextVar("usage_context", default=None)


def check_deadline():
    end = deadline.get()
    if end is not None and time.monotonic() >= end:
        raise HTTPException(504, "Request deadline exceeded; retry with the same request key")


def remaining_timeout(default):
    check_deadline()
    end = deadline.get()
    return min(default, max(0.001, end - time.monotonic())) if end is not None else default


class AsyncStorage:
    """Limit SQLite threads and retain capacity until cancelled calls actually finish."""

    def __init__(self, concurrency=4):
        self.slots = asyncio.Semaphore(concurrency)

    async def __call__(self, function, *args, **kwargs):
        from app.operations import finish_on_disconnect

        async def run():
            async with self.slots:
                return await asyncio.to_thread(function, *args, **kwargs)

        return await finish_on_disconnect(run())


class RequestRuntime:
    def __init__(self, state, settings):
        self.state, self.settings = state, settings
        self.tasks = set()

    async def run(self, work, owner):
        async def execute():
            end = deadline.set(time.monotonic() + self.settings.turn_timeout)
            context = usage_context.set((self.state.usage, owner))
            try:
                return await work()
            finally:
                usage_context.reset(context)
                deadline.reset(end)

        task = asyncio.create_task(execute())
        self.tasks.add(task)

        def completed(result):
            self.tasks.discard(result)
            if not result.cancelled():
                result.exception()  # Consume failures after an HTTP timeout/disconnect.

        task.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.settings.turn_timeout)
        except TimeoutError:
            # Never cancel a thread or free admission while work still runs. Each
            # downstream step and the transaction's commit entry check the deadline.
            raise HTTPException(
                504, "Request deadline exceeded; retry with the same request key"
            ) from None

    async def close(self):
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

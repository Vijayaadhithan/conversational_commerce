"""Bound response bytes before JSON decoding and stop repeated upstream failures."""

import json
import threading
import time

from fastapi import HTTPException

from app.request_runtime import check_deadline, remaining_timeout, usage_context

MAX_RESPONSE_BYTES = 1024 * 1024


async def bounded_lines(response, limit=65536):
    pending, size = bytearray(), 0
    async for chunk in response.aiter_bytes(chunk_size=4096):
        check_deadline()
        size += len(chunk)
        if size > limit:
            raise ValueError("Provider stream exceeds byte limit")
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            yield line.decode("utf-8").rstrip("\r")
    if pending:
        yield pending.decode("utf-8").rstrip("\r")


async def async_json(client, method, path, **kwargs):
    kwargs.setdefault("timeout", remaining_timeout(120))
    async with client.stream(method, path, **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=16384):
            check_deadline()
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Provider response exceeds byte limit")
    return json.loads(data)


def sync_json(client, method, path, *, timeout, **kwargs):
    end = time.monotonic() + remaining_timeout(timeout)
    with client.stream(method, path, timeout=remaining_timeout(timeout), **kwargs) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=16384):
            check_deadline()
            if time.monotonic() >= end:
                raise TimeoutError("Provider response deadline exceeded")
            data.extend(chunk)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Provider response exceeds byte limit")
    return json.loads(data)


class CircuitBreaker:
    def __init__(self, threshold=5, cooldown=30):
        self.threshold, self.cooldown = threshold, cooldown
        self.states = {}
        self.lock = threading.Lock()

    def enter(self, provider):
        with self.lock:
            failures, until, probing = self.states.get(provider, (0, 0, False))
            if until > time.monotonic() or probing:
                raise HTTPException(
                    503,
                    "Provider temporarily unavailable",
                    headers={"Retry-After": str(self.cooldown)},
                )
            if failures >= self.threshold:
                self.states[provider] = (failures, until, True)

    def finish(self, provider, successful):
        with self.lock:
            failures = 0 if successful else self.states.get(provider, (0, 0, False))[0] + 1
            self.states[provider] = (
                failures,
                time.monotonic() + self.cooldown if failures >= self.threshold else 0,
                False,
            )


def circuit():
    context = usage_context.get()
    return context[0].circuit if context else None

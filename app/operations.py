"""Single-worker API admission, bounded telemetry, and local alert evaluation."""

import asyncio
import json
import logging
import math
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import HTTPException

from app.metrics import Metrics as Metrics
from app.operations_config import OperationsSettings as OperationsSettings

log = logging.getLogger("commerce.operations")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.propagate = False


class TokenBuckets:
    def __init__(self):
        self.buckets = OrderedDict()

    def consume(self, key, rate, capacity):
        now = time.monotonic()
        tokens, previous = self.buckets.pop(key, (capacity, now))
        tokens = min(capacity, tokens + (now - previous) * rate / 60)
        allowed = tokens >= 1
        self.buckets[key] = (tokens - 1 if allowed else tokens, now)
        if len(self.buckets) > 4096:
            self.buckets.popitem(last=False)
        return 0 if allowed else max(1, math.ceil((1 - tokens) * 60 / rate))


class Admission:
    def __init__(self, settings, metrics):
        self.settings, self.metrics = settings, metrics
        self.semaphore = asyncio.Semaphore(settings.model_concurrency)
        self.active = self.waiting = 0
        self.conversations = {}

    @asynccontextmanager
    async def workload(self):
        if self.active + self.waiting >= self.settings.model_concurrency + self.settings.queue_size:
            self.metrics.increment("model_rejected_total")
            raise HTTPException(
                503,
                "Model queue is full; retry with the same request key",
                headers={"Retry-After": "5"},
            )
        started = time.monotonic()
        self.waiting += 1
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), self.settings.queue_timeout)
            except TimeoutError as exc:
                self.metrics.increment("model_queue_timeouts_total")
                raise HTTPException(
                    503,
                    "Model queue wait expired; retry with the same request key",
                    headers={"Retry-After": "5"},
                ) from exc
        finally:
            self.waiting -= 1
        self.active += 1
        self.metrics.increment("model_workloads_total")
        self.metrics.increment("model_queue_wait_seconds_total", time.monotonic() - started)
        try:
            yield
        finally:
            self.active -= 1
            self.semaphore.release()

    @asynccontextmanager
    async def conversation(self, key):
        lock, count = self.conversations.get(key, (asyncio.Lock(), 0))
        self.conversations[key] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self.conversations[key]
            if count == 1:
                self.conversations.pop(key)
            else:
                self.conversations[key] = (lock, count - 1)


async def finish_on_disconnect(coroutine):
    """Keep the admission slot until async inference AND blocking threads finish."""
    task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not free capacity while a torch thread or upstream model is still busy.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


class OperationalMiddleware:
    def __init__(self, app, settings, state):
        self.app, self.settings, self.state = app, settings, state
        self.buckets = TokenBuckets()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started, status = time.monotonic(), 500
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def respond(code, detail, headers=()):
            await wrapped_send(
                {
                    "type": "http.response.start",
                    "status": code,
                    "headers": [(b"content-type", b"application/json"), *headers],
                }
            )
            await wrapped_send(
                {"type": "http.response.body", "body": json.dumps({"detail": detail}).encode()}
            )

        async def wrapped_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", request_id.encode())
                ]
            await send(message)

        try:
            headers = dict(scope.get("headers", []))
            public = scope["path"] in {
                "/livez",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/docs/oauth2-redirect",
            }
            if self.settings.auth_required and not public:
                token = headers.get(b"authorization", b"")
                principal = (
                    self.state.store.authenticate(token[7:].decode("latin1"))
                    if token.startswith(b"Bearer ") and len(token) <= 256
                    else None
                )
                if principal is None:
                    retry = self.buckets.consume(("unauthorized",), 60, 30)
                    return await respond(
                        429 if retry else 401,
                        "Invalid or missing bearer token",
                        [
                            (b"www-authenticate", b"Bearer"),
                            (b"retry-after", str(retry or 1).encode()),
                        ],
                    )
            else:
                principal = {"id": "local-development", "role": "admin"}
            scope["state"]["principal"] = principal
            if scope["path"].startswith("/ops/") and principal["role"] != "admin":
                return await respond(403, "Administrator access required")
            retry = self.buckets.consume(
                ("http", principal["id"]),
                self.settings.requests_per_minute,
                self.settings.request_burst,
            )
            if retry:
                return await respond(
                    429, "Request rate limit exceeded", [(b"retry-after", str(retry).encode())]
                )
            # Bound input before JSON decoding, including chunked requests.
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                body = bytearray()
                while True:
                    try:
                        event = await asyncio.wait_for(receive(), timeout=15)
                    except TimeoutError:
                        return await respond(408, "Request body timed out")
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > 262144:
                        return await respond(413, "Request body exceeds 256 KiB")
                    if not event.get("more_body"):
                        break
                consumed = False

                async def replay_receive():
                    nonlocal consumed
                    if not consumed:
                        consumed = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay_receive, wrapped_send)
            else:
                await self.app(scope, receive, wrapped_send)
        finally:
            route = scope.get("route")
            route = getattr(route, "path", "unmatched")
            elapsed = time.monotonic() - started
            self.state.metrics.request(
                route,
                scope["method"]
                if scope["method"] in {"GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"}
                else "OTHER",
                status,
                elapsed,
            )
            log.info(
                json.dumps(
                    {
                        "event": "request_completed",
                        "request_id": request_id,
                        "route": route,
                        "status": status,
                        "seconds": round(elapsed, 4),
                    }
                )
            )

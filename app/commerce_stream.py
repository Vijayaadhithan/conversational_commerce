"""Server-sent progress and validated reply events, with durable disconnect recovery."""

import asyncio
import json

from fastapi import HTTPException

from app.operations import finish_on_disconnect


def event(name, value):
    return f"event: {name}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


class ProgressQueue(asyncio.Queue):
    """Track produced deltas even while a consumer has dequeued but not rendered one."""

    def __init__(self):
        super().__init__(maxsize=64)
        self.delta_text = ""
        self.queued_bytes = 0
        self.overflow = False
        self.disconnected = False

    def put_nowait(self, item):
        if self.disconnected:
            return
        name, value = item
        terminal = name in {"done", "error"}
        size = len(json.dumps(item, ensure_ascii=False).encode())
        # Merge adjacent unsent text while preserving the exact displayed prefix.
        if (
            name == "delta"
            and self._queue
            and self._queue[-1][0] == "delta"
            and len((self.delta_text + value["text"]).encode()) <= 65536
            and self.queued_bytes + size <= 1024 * 1024
        ):
            previous = self._queue[-1]
            merged = ("delta", {"text": previous[1]["text"] + value["text"]})
            self.queued_bytes += len(json.dumps(merged, ensure_ascii=False).encode()) - len(
                json.dumps(previous, ensure_ascii=False).encode()
            )
            self._queue[-1] = merged
            self.delta_text += value["text"]
            return
        if not terminal and (
            self.qsize() >= 62
            or self.queued_bytes + size > 1024 * 1024
            or (name == "delta" and len((self.delta_text + value["text"]).encode()) > 65536)
        ):
            if name == "delta":
                self.overflow = True
            return
        if size > 1024 * 1024:
            self.overflow = True
            if not terminal:
                return
            item = (
                "error",
                {
                    "status": 503,
                    "retry_after": 0,
                    "error": "Stream exceeded its budget; retry with the same key using JSON.",
                },
            )
            size = len(json.dumps(item, ensure_ascii=False).encode())
        super().put_nowait(item)
        self.queued_bytes += size
        if item[0] == "delta":
            self.delta_text += item[1]["text"]

    def get_nowait(self):
        item = super().get_nowait()
        self.queued_bytes -= len(json.dumps(item, ensure_ascii=False).encode())
        return item

    def disconnect(self):
        self.disconnected = True
        while not self.empty():
            self.get_nowait()


async def stream_turn(work, progress):
    async def run():
        try:
            result = await work()
            # Flush thread-safe progress callbacks before the terminal events,
            # including when cached or mocked work completes without suspending.
            await asyncio.sleep(0)
            # Evidence highlights may already be visible; done remains the durable commit.
            prefix = getattr(progress, "delta_text", "")
            if not result["reply"].startswith(prefix):
                raise RuntimeError("Streamed reply differs from committed reply")
            for line in result["reply"][len(prefix) :].splitlines(keepends=True):
                progress.put_nowait(("delta", {"text": line}))
            if getattr(progress, "overflow", False):
                raise RuntimeError("Stream budget exceeded; committed response remains replayable")
            if result.get("commerce"):
                progress.put_nowait(("commerce", result["commerce"]))
            progress.put_nowait(("done", result))
        except Exception as exc:
            status = exc.status_code if isinstance(exc, HTTPException) else 503
            retry = (
                (exc.headers or {}).get("Retry-After", "0")
                if isinstance(exc, HTTPException)
                else "0"
            )
            progress.put_nowait(
                (
                    "error",
                    {
                        "status": status,
                        "retry_after": int(retry) if retry.isdigit() else 0,
                        "error": "This response could not finish. Retry the same message to recover it.",
                    },
                )
            )

    task = asyncio.create_task(run())
    try:
        yield event("status", {"text": "Waiting for your shopping companion…"})
        while True:
            try:
                name, value = await asyncio.wait_for(progress.get(), timeout=10)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield event(name, value)
            if name in {"done", "error"}:
                break
    finally:
        if isinstance(progress, ProgressQueue):
            progress.disconnect()

        # Closing the browser stream must not release admission or the retry key
        # while inference/search is still running. The committed turn is recoverable.
        async def finish():
            await asyncio.shield(task)

        await finish_on_disconnect(finish())

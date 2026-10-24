"""Server-sent progress and validated reply events, with durable disconnect recovery."""

import asyncio
import json

from fastapi import HTTPException

from app.operations import finish_on_disconnect


def event(name, value):
    return f"event: {name}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


async def stream_turn(work, progress):
    async def run():
        try:
            result = await work()
            # Flush thread-safe progress callbacks before the terminal events,
            # including when cached or mocked work completes without suspending.
            await asyncio.sleep(0)
            # Only committed, validated output is exposed, never partial model JSON.
            for line in result["reply"].splitlines(keepends=True):
                progress.put_nowait(("delta", {"text": line}))
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
        # Closing the browser stream must not release admission or the retry key
        # while inference/search is still running. The committed turn is recoverable.
        async def finish():
            await asyncio.shield(task)

        await finish_on_disconnect(finish())

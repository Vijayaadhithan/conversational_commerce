"""Stream output uses committed data, preserves retries, and completes after disconnect."""

import asyncio
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_commerce import Search, harness, new, plan

from app.commerce_stream import stream_turn


def frames(text):
    return [
        (part.splitlines()[0][7:], json.loads(part.splitlines()[1][6:]))
        for part in text.strip().split("\n\n")
        if part.startswith("event:")
    ]


def test_streamed_turn_replay_matches_json_and_commits_once(tmp_path):
    message = "shampoo under $30"
    _, app, calls = harness(
        tmp_path, {message: plan("search", message, recommendation_requested=True)}
    )
    with TestClient(app) as client:
        app.state.product_search = Search()
        cid = new(client)
        url = f"/conversations/{cid}/messages"
        headers = {"Accept": "text/event-stream", "Idempotency-Key": "stream-test-message"}
        response = client.post(url, json={"message": message}, headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = frames(response.text)
        assert events[0][0] == "status"
        assert any(
            "Finding products" in data["text"] for name, data in events if name == "status"
        ), events
        result = events[-1][1]
        assert events[-1][0] == "done" and result["commerce"]["products"]
        assert "".join(data["text"] for name, data in events if name == "delta") == result["reply"]
        assert next(data for name, data in events if name == "commerce") == result["commerce"]
        replay = client.post(url, json={"message": message}, headers=headers)
        assert frames(replay.text)[-1] == ("done", result)
        ordinary = client.post(
            url, json={"message": message}, headers={"Idempotency-Key": "stream-test-message"}
        )
        assert ordinary.json() == result and len(calls) == 1
        assert len(client.get(url).json()["messages"]) == 2


def test_stream_reports_safe_errors_and_retry_delay():
    async def check():
        async def work():
            raise HTTPException(429, "private upstream details", headers={"Retry-After": "8"})

        output = "".join([frame async for frame in stream_turn(work, asyncio.Queue())])
        assert "private upstream" not in output
        name, data = frames(output)[-1]
        assert name == "error" and data["status"] == 429 and data["retry_after"] == 8

    asyncio.run(check())


def test_stream_closing_waits_for_work_instead_of_cancelling_it():
    async def check():
        started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def work():
            started.set()
            await release.wait()
            completed.set()
            return {"reply": "Saved", "message_id": 2}

        stream = stream_turn(work, asyncio.Queue())
        assert "status" in await anext(stream)
        await started.wait()
        closing = asyncio.create_task(stream.aclose())
        await asyncio.sleep(0)
        assert not closing.done() and not completed.is_set()
        release.set()
        await closing
        assert completed.is_set()

    asyncio.run(check())

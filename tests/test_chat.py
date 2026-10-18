import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

from fastapi.testclient import TestClient
import httpx

from app.main import Settings, create_app


def setup(tmp_path, handler=None):
    calls = []

    def default(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "ministral-3:8b"}]})
        body = json.loads(request.content)
        calls.append(body)
        is_summary = body["messages"][0]["content"].startswith("Update a compact")
        answer = "The user is named Ada and likes blue." if is_summary else "Hello Ada."
        return httpx.Response(200, json={"message": {"content": answer}, "done": True, "done_reason": "stop"})

    config = replace(Settings(), database=str(tmp_path / "chat.sqlite3"))
    transport = httpx.MockTransport(handler or default)
    return config, transport, calls


def new(client):
    response = client.post("/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


def send(client, session, text="My name is Ada"):
    return client.post(f"/conversations/{session}/messages", json={"message": text})


def test_persistence_and_session_isolation(tmp_path):
    config, transport, calls = setup(tmp_path)
    with TestClient(create_app(config, transport)) as client:
        first, other = new(client), new(client)
        assert client.get("/health").status_code == 200
        assert send(client, first).status_code == 200
        assert send(client, first, "What is my name?").json()["reply"] == "Hello Ada."
        assert [m["role"] for m in calls[-1]["messages"]] == ["system", "user", "assistant", "user"]
        assert client.get(f"/conversations/{other}/messages").json()["messages"] == []
        assert len(client.get("/conversations").json()["conversations"]) == 2

    # A fresh app instance reads the same persisted history after restart.
    with TestClient(create_app(config, transport)) as client:
        assert len(client.get(f"/conversations/{first}/messages").json()["messages"]) == 4
        assert send(client, first, "Continue").status_code == 200
        assert calls[-1]["messages"][1]["content"] == "My name is Ada"
        page = client.get(f"/conversations/{first}/messages?limit=2").json()
        assert len(client.get(f'/conversations/{first}/messages?after={page["next_after"]}').json()["messages"]) == 4
        assert client.delete(f"/conversations/{first}").status_code == 204
        assert client.get(f"/conversations/{first}").status_code == 404
        assert send(client, first).status_code == 404


def test_long_chat_compacts_memory_and_preserves_transcript(tmp_path):
    config, transport, calls = setup(tmp_path)
    with TestClient(create_app(config, transport)) as client:
        session = new(client)
        for i in range(18):
            assert send(client, session, f"Turn {i}: I like blue.").status_code == 200
        record = client.get(f"/conversations/{session}").json()
        assert record["summary"] == "The user is named Ada and likes blue."
        assert record["summarized_through"] > 0
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 36
        assert any("Earlier conversation summary" in m["content"] for m in calls[-1]["messages"])
        assert len(calls[-1]["messages"]) <= 15
        assert all(len(json.dumps(c["messages"], ensure_ascii=False).encode()) <= config.prompt_budget for c in calls)


def test_byte_budget_compacts_unicode_history(tmp_path):
    config, transport, calls = setup(tmp_path)
    with TestClient(create_app(config, transport)) as client:
        session = new(client)
        for _ in range(6):
            assert send(client, session, "நன்றி " * 200).status_code == 200
        assert client.get(f"/conversations/{session}").json()["summarized_through"] > 0
        assert all(len(json.dumps(c["messages"], ensure_ascii=False).encode()) <= config.prompt_budget for c in calls)


def test_provider_failure_does_not_save_half_a_turn(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    config, transport, _ = setup(tmp_path, handler)
    with TestClient(create_app(config, transport)) as client:
        session = new(client)
        assert send(client, session).status_code == 504
        assert client.get(f"/conversations/{session}/messages").json()["messages"] == []


def test_invalid_completed_response_and_input(tmp_path):
    config, transport, _ = setup(tmp_path, lambda _: httpx.Response(200, json={"done": False}))
    with TestClient(create_app(config, transport)) as client:
        session = new(client)
        assert send(client, session).status_code == 502
        for bad in ["", "  ", "a" * 4001, "🙂" * 1001]:
            assert send(client, session, bad).status_code == 422
        assert client.get(f"/conversations/{session}/messages").json()["messages"] == []


def test_simultaneous_turns_see_previous_completed_turn(tmp_path):
    observed = []

    async def handler(request):
        body = json.loads(request.content)
        observed.append(body["messages"])
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"done": True, "message": {"content": "Reply"}})

    config, transport, _ = setup(tmp_path, handler)
    with TestClient(create_app(config, transport)) as client:
        session = new(client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda text: send(client, session, text), ["First", "Second"]))
        assert all(r.status_code == 200 for r in results)
        assert [len(messages) for messages in observed] == [2, 4]


def test_failed_reply_rolls_back_new_summary(tmp_path):
    fail_reply = False

    def handler(request):
        body = json.loads(request.content)
        is_summary = body["messages"][0]["content"].startswith("Update a compact")
        if fail_reply and not is_summary:
            return httpx.Response(500, json={"error": "inference failed"})
        return httpx.Response(200, json={"done": True, "message": {
            "content": "Ada likes blue." if is_summary else "OK"}})

    config, transport, _ = setup(tmp_path, handler)
    with TestClient(create_app(config, transport)) as client:
        session = new(client)
        for _ in range(7):
            assert send(client, session).status_code == 200
        fail_reply = True
        assert send(client, session).status_code == 502
        record = client.get(f"/conversations/{session}").json()
        assert record["summary"] == "" and record["summarized_through"] == 0
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 14

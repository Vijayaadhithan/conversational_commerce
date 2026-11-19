"""Operational contracts with controlled model latency/failures, no external services."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, Store, create_app
from app.operations import Admission, Metrics, OperationsSettings, finish_on_disconnect


@pytest.fixture
def secured(tmp_path):
    settings = replace(Settings(), database=str(tmp_path / "secure.sqlite3"))
    store = Store(settings.database)
    identities = {}
    for name, role in [("alice", "user"), ("bob", "user"), ("admin", "admin")]:
        uid, token = store.issue_token(name, role)
        identities[name] = {"Authorization": "Bearer " + token}
    ops = replace(OperationsSettings(), monitor_interval=0, request_burst=1000, model_burst=1000)
    return settings, store, identities, ops


def response(request):
    return httpx.Response(200, json={"done": True, "message": {"content": "A completed answer"}})


def build(secured, handler=response, **limits):
    settings, store, users, ops = secured
    return create_app(settings, httpx.MockTransport(handler), replace(ops, **limits))


def create(client, users, name="alice", key="create-0001"):
    return client.post("/conversations", json={}, headers={**users[name], "Idempotency-Key": key})


def send(client, users, cid, key="message-0001", text="hello", name="alice"):
    return client.post(
        f"/conversations/{cid}/messages",
        json={"message": text},
        headers={**users[name], "Idempotency-Key": key},
    )


def test_auth_ownership_revocation_and_legacy(secured):
    _, store, users, _ = secured
    legacy = store.create("old")
    with store.connect() as db:
        db.execute("UPDATE conversations SET owner_id=NULL WHERE id=?", (legacy["id"],))
    with TestClient(build(secured)) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/conversations").status_code == 401
        cid = create(client, users).json()["id"]
        for name in ("bob", "admin"):
            for suffix in ("", "/messages", "/preferences"):
                assert (
                    client.get(f"/conversations/{cid}{suffix}", headers=users[name]).status_code
                    == 404
                )
            assert client.delete(f"/conversations/{cid}", headers=users[name]).status_code == 404
            assert send(client, users, cid, name=name).status_code == 404
            assert client.get("/conversations", headers=users[name]).json()["conversations"] == []
        assert (
            client.get("/conversations/" + legacy["id"], headers=users["alice"]).status_code == 404
        )
        assert client.get("/ops/metrics", headers=users["alice"]).status_code == 403
        assert client.get("/ops/metrics", headers=users["admin"]).status_code == 200
        store.revoke_tokens("alice")
        assert client.get("/conversations", headers=users["alice"]).status_code == 401


def test_durable_replay_and_conflicting_payload(secured):
    _, store, users, _ = secured
    calls = []

    def model(request):
        calls.append(request)
        return response(request)

    app = build(secured, model)
    with TestClient(app) as client:
        assert client.post("/conversations", json={}, headers=users["alice"]).status_code == 400
        first = create(client, users)
        assert first.status_code == 201
        assert create(client, users).json() == first.json()
        cid = first.json()["id"]
        answer = send(client, users, cid)
        assert answer.status_code == 200
        replay = send(client, users, cid)
        assert replay.json() == answer.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert send(client, users, cid, text="changed").status_code == 409
        assert len(calls) == 1
        assert len(store.messages(cid)) == 2
    with TestClient(build(secured, model)) as client:
        assert send(client, users, cid).json() == answer.json()
        assert len(calls) == 1


def test_inflight_duplicate_and_failed_request_retry(secured):
    _, store, users, _ = secured
    started, release = threading.Event(), threading.Event()
    fail = [False]

    async def model(request):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return httpx.Response(500) if fail[0] else response(request)

    with TestClient(build(secured, model)) as client, ThreadPoolExecutor() as pool:
        cid = create(client, users).json()["id"]
        future = pool.submit(send, client, users, cid)
        assert started.wait(3)
        duplicate = send(client, users, cid)
        assert duplicate.status_code == 409 and duplicate.headers["Retry-After"]
        release.set()
        assert future.result().status_code == 200
        assert len(store.messages(cid)) == 2
        fail[0] = True
        assert send(client, users, cid, key="message-failed").status_code == 502
        assert len(store.messages(cid)) == 2
        fail[0] = False
        assert send(client, users, cid, key="message-failed").status_code == 200
        assert len(store.messages(cid)) == 4


def test_http_and_model_rate_limits(secured):
    _, _, users, _ = secured
    with TestClient(build(secured, request_burst=1, requests_per_minute=1)) as client:
        assert client.get("/conversations", headers=users["alice"]).status_code == 200
        limited = client.get("/conversations", headers=users["alice"])
        assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
        assert client.get("/conversations", headers=users["bob"]).status_code == 200
    with TestClient(build(secured, model_burst=1, model_requests_per_minute=1)) as client:
        cid = create(client, users, key="create-limit").json()["id"]
        assert send(client, users, cid).status_code == 200
        assert send(client, users, cid).status_code == 200  # replay never invokes the model
        assert send(client, users, cid, key="message-next").status_code == 429


def test_stream_error_counts_as_workload_failure_and_same_key_can_retry(secured):
    _, store, users, _ = secured
    fail = True

    def model(request):
        return httpx.Response(500) if fail else response(request)

    app = build(secured, model)
    with TestClient(app) as client:
        cid = create(client, users).json()["id"]
        url = f"/conversations/{cid}/messages"
        headers = {
            **users["alice"],
            "Accept": "text/event-stream",
            "Idempotency-Key": "stream-failed",
        }
        failed = client.post(url, json={"message": "hello"}, headers=headers)
        assert failed.status_code == 200
        assert "event: error" in failed.text and '"status": 502' in failed.text
        assert store.messages(cid) == []
        status = client.get("/ops/status", headers=users["admin"]).json()
        assert status["error_ratio"] == 0.5
        metrics = client.get("/ops/metrics", headers=users["admin"]).text
        assert 'commerce_stream_turns_total{outcome="error",status="502"} 1' in metrics
        assert (
            'commerce_http_requests_total{method="POST",route="/conversations/{conversation_id}/messages",status="200"} 1'
            in metrics
        )
        assert "commerce_stream_done_seconds_count 0" in metrics
        fail = False
        assert "event: done" in client.post(url, json={"message": "hello"}, headers=headers).text
        assert len(store.messages(cid)) == 2


def test_bounded_queue_and_per_conversation_serialization(secured):
    _, store, users, _ = secured
    started, release = threading.Event(), threading.Event()
    contexts = []

    async def model(request):
        import json

        contexts.append(len(json.loads(request.content)["messages"]))
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return response(request)

    app = build(secured, model, queue_size=1, queue_timeout=0.2)
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        cid = create(client, users).json()["id"]
        future = pool.submit(send, client, users, cid)
        assert started.wait(3)
        waiting = pool.submit(send, client, users, cid, "message-queued")
        deadline = time.monotonic() + 2
        while app.state.admission.waiting != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        full = send(client, users, cid, key="message-full")
        assert full.status_code == 503 and full.headers["Retry-After"]
        assert waiting.result().status_code == 503
        release.set()
        assert future.result().status_code == 200
        assert send(client, users, cid, key="message-queued").status_code == 200
        assert contexts == [2, 4]
        assert len(store.messages(cid)) == 4


def test_second_worker_is_refused_and_abandoned_claim_recovered(secured):
    _, store, users, _ = secured
    uid = store.authenticate(users["alice"]["Authorization"][7:])["id"]
    store.claim_request(uid, "create", "orphaned-key", {}, True)
    with TestClient(build(secured)) as client:
        with pytest.raises(RuntimeError, match="One API worker"):
            with TestClient(build(secured)):
                pass
        assert create(client, users, key="orphaned-key").status_code == 201


def test_cancellation_retains_model_slot_until_thread_finishes():
    async def run():
        metrics = Metrics()
        admission = Admission(replace(OperationsSettings(), queue_size=0), metrics)
        release, started = threading.Event(), threading.Event()

        def blocking():
            started.set()
            release.wait(5)

        async def work():
            async with admission.workload():
                await asyncio.to_thread(blocking)

        task = asyncio.create_task(finish_on_disconnect(work()))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert admission.active == 1
        with pytest.raises(Exception) as exc:
            async with admission.workload():
                pass
        assert exc.value.status_code == 503
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admission.active == 0

    asyncio.run(run())


def test_alerts_metrics_and_bounded_body(secured, monkeypatch):
    _, _, users, _ = secured
    app = build(secured)

    async def probes(state, model):
        state.metrics.dependencies = {
            "sqlite": {"status": "ready"},
            "postgres": {"status": "unavailable"},
            "models": {"status": "ready"},
            "reranker": {"status": "ready"},
            "redis": {"status": "unavailable"},
        }

    monkeypatch.setattr("app.main.probe_dependencies", probes)
    with TestClient(app) as client:
        assert client.get("/ready", headers=users["alice"]).status_code == 503
        status = client.get("/ops/status", headers=users["admin"]).json()
        assert "postgres_unavailable" in status["active_alerts"]
        assert status["resources"]["rss_bytes"] > 0
        metrics = client.get("/ops/metrics", headers=users["admin"]).text
        assert 'commerce_dependency_up{component="postgres"} 0' in metrics
        assert (
            client.post("/conversations", content=b"x" * 262145, headers=users["alice"]).status_code
            == 413
        )
        for _ in range(10):
            app.state.metrics.request("/products/search", "POST", 503, 70)
        alerts = app.state.metrics.evaluate(OperationsSettings(), app.state.admission)[
            "active_alerts"
        ]
        assert {"high_error_ratio", "high_request_latency"}.issubset(alerts)
        assert users["alice"]["Authorization"][7:] not in metrics


def test_commerce_replay_does_not_advance_state(secured):
    _, store, users, _ = secured
    with TestClient(build(secured)) as client:
        created = client.post(
            "/conversations",
            json={"mode": "commerce"},
            headers={**users["alice"], "Idempotency-Key": "commerce-create"},
        )
        cid = created.json()["id"]
        first = send(client, users, cid, text="reset")
        assert first.status_code == 200
        assert send(client, users, cid, text="reset").json() == first.json()
        assert store.get(cid)["state_version"] == 1
        assert len(store.messages(cid)) == 2


def test_atomic_failure_rolls_back_messages_and_request(secured, monkeypatch):
    _, store, users, _ = secured
    app = build(secured)
    with TestClient(app, raise_server_exceptions=False) as client:
        cid = create(client, users).json()["id"]
        original = app.state.store.complete_request

        def broken(*args, **kwargs):
            raise RuntimeError("Simulated commit failure")

        monkeypatch.setattr(app.state.store, "complete_request", broken)
        assert send(client, users, cid).status_code == 500
        assert store.messages(cid) == []
        monkeypatch.setattr(app.state.store, "complete_request", original)
        assert send(client, users, cid).status_code == 200
        assert len(store.messages(cid)) == 2


def test_a_queued_turn_does_not_take_a_model_slot_from_another_conversation(secured):
    _, _, users, _ = secured
    first_started, release = threading.Event(), threading.Event()
    contexts = {}

    async def model(request):
        import json

        messages = json.loads(request.content)["messages"]
        text = messages[-1]["content"]
        contexts[text] = len(messages)
        if text == "first":
            first_started.set()
            await asyncio.to_thread(release.wait, 5)
        return response(request)

    app = build(secured, model, model_concurrency=2, queue_size=1)
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        cid = create(client, users).json()["id"]
        other = create(client, users, name="bob").json()["id"]
        first = pool.submit(send, client, users, cid, "parallel-first", "first")
        try:
            assert first_started.wait(2)
            second = pool.submit(send, client, users, cid, "parallel-second", "second")
            deadline = time.monotonic() + 2
            admission = app.state.admission
            while admission.waiting != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert admission.waiting == 1 and admission.active == 1
            independent = pool.submit(send, client, users, other, "other-turn", "other", "bob")
            assert independent.result(timeout=2).status_code == 200
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=2).status_code == 200
        assert second.result(timeout=2).status_code == 200
        assert contexts == {"first": 2, "other": 2, "second": 4}


def test_schema_documents_authentication_and_keys(secured):
    with TestClient(build(secured)) as client:
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        operation = schema["paths"]["/conversations/{conversation_id}/messages"]["post"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert next(p for p in operation["parameters"] if p["name"] == "Idempotency-Key")[
            "required"
        ]
        assert client.get("/docs").status_code == 200

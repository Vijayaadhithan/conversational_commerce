"""Security and recovery contracts using scratch SQLite and mocked upstreams only."""

import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api_access import RequestAccess
from app.api_models import NewConversation
from app.commerce_stream import ProgressQueue, stream_turn
from app.conversation_maintenance import backup_sqlite, readonly, restore_sqlite, retention
from app.conversation_store import Store
from app.main import Settings, create_app
from app.operations import Admission, Metrics, OperationalMiddleware, OperationsSettings
from app.provider_safety import CircuitBreaker, async_json
from app.request_runtime import AsyncStorage, RequestRuntime, check_deadline
from app.usage_budget import UsageBudget


def test_deleted_conversation_erases_replays_without_reexecuting(tmp_path):
    store = Store(tmp_path / "chat.db")
    owner, token = store.issue_token("alice")
    create, _ = store.claim_request(owner, "create", "create-key", {})
    record = store.create("private title", owner=owner, identity=create)
    identity, _ = store.claim_request(owner, "message:" + record["id"], "message-key", {})
    store.commit_turn(
        record["id"],
        "private question",
        "private reply",
        "",
        0,
        identity,
        {"conversation_id": record["id"], "reply": "private reply"},
    )
    store.delete(record["id"])
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM api_requests WHERE response IS NOT NULL").fetchone()[0]
            == 0
        )
    store = Store(store.path)  # Migration and crash recovery preserve tombstones.
    store.recover_requests()
    for operation, key in [("create", "create-key"), ("message:" + record["id"], "message-key")]:
        with pytest.raises(HTTPException) as error:
            store.claim_request(owner, operation, key, {})
        assert error.value.status_code == 410
    assert store.authenticate(token)


def test_token_expiry_scope_and_individual_revocation(tmp_path):
    settings = replace(Settings(), database=str(tmp_path / "chat.db"))
    store = Store(settings.database)
    _, first = store.issue_token("alice", expires_in=60, scopes="shopping", label="first")
    _, second = store.issue_token("alice", expires_in=60, scopes="shopping", label="second")
    tokens = store.list_tokens()
    assert all("digest" not in token for token in tokens)
    store.revoke_token(next(token["token_id"] for token in tokens if token["label"] == "first"))
    assert store.authenticate(first) is None and store.authenticate(second)
    with TestClient(
        create_app(settings, operations=replace(OperationsSettings(), monitor_interval=0))
    ) as client:
        denied = client.post("/rerank", headers={"Authorization": "Bearer " + second}, json={})
        assert denied.status_code == 403
    with store.connect() as db:
        db.execute("UPDATE api_tokens SET expires_at=?", (time.time() - 1,))
    assert store.authenticate(second) is None


def test_retention_preview_apply_backup_and_new_path_restore(tmp_path):
    store = Store(tmp_path / "chat.db")
    owner, _ = store.issue_token("alice")
    identity, _ = store.claim_request(owner, "create", "create-key", {})
    record = store.create("old", owner=owner, identity=identity)
    with store.connect() as db:
        db.execute("UPDATE conversations SET updated_at='2000-01-01T00:00:00+00:00'")
    snapshot = tmp_path / "snapshot.db"
    backup_sqlite(store.path, snapshot)
    with readonly(store.path) as db:
        assert retention(db)["conversation_ids"] == [record["id"]]
    assert store.get(record["id"])
    with store.connect() as db:
        retention(db, apply=True)
    with pytest.raises(HTTPException):
        store.get(record["id"])
    restored = tmp_path / "restored.db"
    restore_sqlite(snapshot, restored)
    assert Store(restored).get(record["id"])["title"] == "old"
    with pytest.raises(ValueError):
        restore_sqlite(snapshot, restored)


def test_budgets_persist_unknown_usage_and_isolate_users(tmp_path, monkeypatch):
    monkeypatch.setenv("API_DAILY_PROVIDER_CALLS", "3")
    monkeypatch.setenv("API_USER_DAILY_PROVIDER_CALLS", "1")
    store = Store(tmp_path / "chat.db")
    budget = UsageBudget(store)
    budget.reserve("alice", "groq", "demo", "llm")
    budget = UsageBudget(store)
    with pytest.raises(HTTPException) as error:
        budget.reserve("alice", "groq", "demo", "llm")
    assert error.value.status_code == 429
    ticket = budget.reserve("bob", "groq", "demo", "llm")
    budget.finish(ticket, {"prompt_eval_count": 10, "eval_count": 2})
    row = budget.summary()["providers"][0]
    assert row["calls"] == 2 and row["unknown_usage_calls"] == 1


def test_turn_timeout_retains_admission_and_prevents_late_commit(tmp_path):
    async def run():
        settings = replace(OperationsSettings(), turn_timeout=0.02)
        state = SimpleNamespace(usage=UsageBudget(Store(tmp_path / "chat.db")))
        runtime = RequestRuntime(state, settings)
        admission = Admission(settings, Metrics())
        release = asyncio.Event()

        async def work():
            async with admission.workload("alice"):
                await release.wait()
                check_deadline()
                raise AssertionError("Expired work must not reach commit")

        with pytest.raises(HTTPException) as error:
            await runtime.run(work, "alice")
        assert error.value.status_code == 504 and admission.active == 1
        release.set()
        await runtime.close()
        assert admission.active == 0

    asyncio.run(run())


def test_slow_upload_has_total_deadline(tmp_path):
    async def run():
        settings = replace(OperationsSettings(), auth_required=False, body_timeout=0.03)
        state = SimpleNamespace(metrics=Metrics(), db_call=AsyncStorage())
        received = []

        async def app(scope, receive, send):
            raise AssertionError("Incomplete slow body must never reach the application")

        async def receive():
            await asyncio.sleep(0.01)
            return {"type": "http.request", "body": b"a", "more_body": True}

        async def send(message):
            received.append(message)

        middleware = OperationalMiddleware(app, settings, state)
        await middleware(
            {"type": "http", "method": "POST", "path": "/conversations", "headers": []},
            receive,
            send,
        )
        assert received[0]["status"] == 408 and middleware.inflight == 0

    asyncio.run(run())


def test_provider_response_byte_limit_and_circuit():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))
            )
        ) as client:
            with pytest.raises(ValueError, match="byte limit"):
                await async_json(client, "GET", "https://example.test/model")

    asyncio.run(run())
    breaker = CircuitBreaker(threshold=2, cooldown=30)
    for _ in range(2):
        breaker.enter("groq")
        breaker.finish("groq", False)
    with pytest.raises(HTTPException) as error:
        breaker.enter("groq")
    assert error.value.status_code == 503


def test_readiness_requires_qdrant_and_caches_probes(tmp_path, monkeypatch):
    calls = []

    async def probe(state, model):
        calls.append(model)
        state.metrics.dependencies = {
            name: {"status": "ready"} for name in ("sqlite", "postgres", "models", "reranker")
        }
        state.metrics.dependencies["qdrant"] = {"status": "unavailable"}

    monkeypatch.setattr("app.main.probe_dependencies", probe)
    with TestClient(create_app(replace(Settings(), database=str(tmp_path / "chat.db")))) as client:
        assert client.get("/ready").status_code == 503
        assert client.get("/ready").status_code == 503
        assert len(calls) == 1


def test_stream_overflow_preserves_terminal_recovery_and_disconnect_bounds():
    async def run():
        queue = ProgressQueue()

        async def work():
            for _ in range(1000):
                queue.put_nowait(("delta", {"text": "x" * 100}))
            assert queue.qsize() <= 62
            return {"reply": "x" * 100000, "message_id": 2}

        output = "".join([frame async for frame in stream_turn(work, queue)])
        assert "event: error" in output and "event: done" not in output
        assert queue.empty() and queue.disconnected
        queue.put_nowait(("delta", {"text": "ignored"}))
        assert queue.empty()

    asyncio.run(run())


def test_per_user_admission_does_not_block_another_user():
    async def run():
        settings = replace(OperationsSettings(), model_concurrency=2, user_workloads=1)
        admission = Admission(settings, Metrics())
        async with admission.workload("alice"):
            with pytest.raises(HTTPException) as error:
                async with admission.workload("alice"):
                    pass
            assert error.value.status_code == 429
            async with admission.workload("bob"):
                assert admission.active == 2
        assert not admission.owners

    asyncio.run(run())


def test_cancelled_claim_is_released_without_restarting(tmp_path):
    async def run():
        store = Store(tmp_path / "chat.db")
        owner, _ = store.issue_token("alice")
        entered, release = threading.Event(), threading.Event()
        original = store.claim_request

        def claim(*args):
            result = original(*args)
            entered.set()
            release.wait(2)
            return result

        store.claim_request = claim
        state = SimpleNamespace(store=store, db_call=AsyncStorage(), metrics=Metrics())
        access = RequestAccess(state, OperationsSettings())
        request = SimpleNamespace(
            state=SimpleNamespace(principal={"id": owner}),
            headers={"Idempotency-Key": "cancelled-create"},
        )
        task = asyncio.create_task(access.claim(request, "create", NewConversation()))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM api_requests").fetchone()[0] == 0

    asyncio.run(run())


def test_recovery_bundle_verification_rejects_tampering(tmp_path):
    from scripts.backup_demo import digest, verify

    data = tmp_path / "catalog.dump"
    data.write_bytes(b"example backup")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "created_at": "fixture",
                "files": {"catalog.dump": digest(data)},
            }
        )
    )
    assert verify(tmp_path)["verified_files"] == 1
    data.write_bytes(b"modified backup")
    with pytest.raises(ValueError):
        verify(tmp_path)


def test_estimated_budget_reservation_survives_unknown_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("API_DAILY_ESTIMATED_USD", "0.02")
    monkeypatch.setenv("LLM_CALL_RESERVE_USD", "0.02")
    monkeypatch.setenv("RERANK_CALL_RESERVE_USD", "0.01")
    budget = UsageBudget(Store(tmp_path / "chat.db"))
    ticket = budget.reserve("alice", "groq", "demo", "llm")
    budget.finish(ticket)  # No reported tokens: retain the reservation.
    with pytest.raises(HTTPException):
        budget.reserve("bob", "voyage", "demo", "rerank")
    assert budget.summary()["providers"][0]["estimated_or_reserved_usd"] == 0.02


def test_upgrade_keeps_old_history_tokens_ownership_and_resume(tmp_path):
    """Exercise the pre-hardening schema, not only a restart of the new schema."""
    settings = replace(Settings(), database=str(tmp_path / "old.db"))
    store = Store(settings.database)
    owner, token = store.issue_token("alice")
    _, other_token = store.issue_token("bob")
    record = store.create("Older conversation", owner=owner)
    key = record["id"]
    body = {"message": "Before the upgrade"}
    identity, _ = store.claim_request(owner, "message:" + key, "legacy-message", body)
    stored_reply = {"conversation_id": key, "reply": "Saved earlier"}
    store.commit_turn(key, body["message"], "Saved earlier", "", 0, identity, stored_reply)
    original_history = store.messages(key)
    with store.connect() as db:
        db.execute("UPDATE conversations SET updated_at='2000-01-01T00:00:00+00:00'")
        db.execute("DROP INDEX tokens_id")
        db.execute("DROP INDEX requests_conversation")
        for table, columns in {
            "api_tokens": ("token_id", "label", "expires_at", "scopes"),
            "api_requests": ("conversation_id", "expired"),
        }.items():
            for column in columns:
                db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    def model(request):
        return httpx.Response(200, json={"done": True, "message": {"content": "Continued"}})

    operations = replace(OperationsSettings(), monitor_interval=0)
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(settings, httpx.MockTransport(model), operations)) as client:
        sessions = client.get("/conversations", headers=headers).json()["conversations"]
        assert [item["id"] for item in sessions] == [key]
        assert (
            client.get(f"/conversations/{key}/messages", headers=headers).json()["messages"]
            == original_history
        )
        assert (
            client.get(
                f"/conversations/{key}", headers={"Authorization": "Bearer " + other_token}
            ).status_code
            == 404
        )
        replay = client.post(
            f"/conversations/{key}/messages",
            json=body,
            headers={**headers, "Idempotency-Key": "legacy-message"},
        )
        assert replay.json() == stored_reply and replay.headers["Idempotency-Replayed"] == "true"
        resumed = client.post(
            f"/conversations/{key}/messages",
            json={"message": "Continue"},
            headers={**headers, "Idempotency-Key": "after-upgrade"},
        )
        assert resumed.status_code == 200 and resumed.json()["reply"] == "Continued"
    reopened = Store(settings.database)
    assert reopened.messages(key)[:2] == original_history
    assert len(reopened.messages(key)) == 4
    assert reopened.authenticate(token)["id"] == owner

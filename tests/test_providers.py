"""Hosted provider contracts and fail-closed behavior; no paid network calls."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalog import CatalogError
from app.embeddings import EmbeddingSettings, create_embedder, query_vector
from app.main import Settings, Store, create_app
from app.operations import OperationsSettings
from app.providers import KEY_NAMES, credential
from app.reranker import VoyageReranker, create_reranker


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    for name in KEY_NAMES.values():
        monkeypatch.delenv(name + "_FILE", raising=False)
        monkeypatch.setenv(name, "test-secret")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("RERANK_PROVIDER", "local")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")


def completion(content="Hello", reason="stop"):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": reason}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
    }


@pytest.mark.parametrize(
    "provider,host,path",
    [
        ("google", "generativelanguage.googleapis.com", "/v1beta/openai/chat/completions"),
        ("groq", "api.groq.com", "/openai/v1/chat/completions"),
        ("openrouter", "openrouter.ai", "/api/v1/chat/completions"),
    ],
)
def test_hosted_chat_security_persistence_and_json_planner(tmp_path, provider, host, path):
    calls = []

    def handler(request):
        assert request.url.host == host and request.url.scheme == "https"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert not request.url.query
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == path and body["model"] == "selected-model"
        assert "keep_alive" not in body and "options" not in body
        if provider == "openrouter":
            assert body["provider"] == {
                "allow_fallbacks": False,
                "data_collection": "deny",
                "require_parameters": True,
            }
        if "response_format" in body:
            assert body["response_format"] == {"type": "json_object"}
            assert "JSON Schema" in body["messages"][0]["content"]
            return httpx.Response(
                200,
                json=completion(
                    json.dumps(
                        {
                            "intent": "clarify",
                            "evidence": "I have dry skin",
                            "changes": [
                                {"field": "need", "value": "dry skin", "evidence": "dry skin"}
                            ],
                            "recommendation_requested": False,
                        }
                    )
                ),
            )
        return httpx.Response(200, json=completion())

    settings = replace(
        Settings(), provider=provider, model="selected-model", database=str(tmp_path / "test.db")
    )
    store = Store(settings.database)
    _, token = store.issue_token("alice")
    app = create_app(
        settings, httpx.MockTransport(handler), replace(OperationsSettings(), monitor_interval=0)
    )
    with TestClient(app) as client:
        assert client.post("/conversations", json={}).status_code == 401
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "create-chat"}
        cid = client.post("/conversations", json={}, headers=headers).json()["id"]
        headers["Idempotency-Key"] = "send-chat"
        result = client.post(
            f"/conversations/{cid}/messages", json={"message": "hello"}, headers=headers
        )
        assert result.status_code == 200 and result.json()["reply"] == "Hello"
        assert result.json()["usage"] == {"prompt_tokens": 20, "completion_tokens": 5}
        assert (
            client.post(
                f"/conversations/{cid}/messages", json={"message": "hello"}, headers=headers
            ).json()
            == result.json()
        )
        assert len(calls) == 1
        headers["Idempotency-Key"] = "create-commerce"
        cid = client.post("/conversations", json={"mode": "commerce"}, headers=headers).json()["id"]
        headers["Idempotency-Key"] = "send-commerce"
        result = client.post(
            f"/conversations/{cid}/messages", json={"message": "I have dry skin"}, headers=headers
        )
        assert result.status_code == 200 and result.json()["commerce"]["action"] == "clarify"
        assert len(calls) == 2


@pytest.mark.parametrize(
    "mode,status",
    [
        ("rate", 503),
        ("unauthorized", 502),
        ("redirect", 502),
        ("timeout", 504),
        ("length", 502),
        ("blocked", 502),
        ("badjson", 502),
        ("empty", 502),
    ],
)
def test_provider_failure_is_sanitized_and_does_not_save_turn(tmp_path, mode, status, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("test-secret prompt-data", request=request)
        if mode == "rate":
            return httpx.Response(429, text="test-secret prompt-data")
        if mode == "unauthorized":
            return httpx.Response(401, text="test-secret prompt-data")
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://example.com/steal"})
        if mode == "badjson":
            return httpx.Response(200, text="test-secret prompt-data")
        return httpx.Response(
            200,
            json=completion(
                "" if mode == "empty" else "answer",
                "length" if mode == "length" else "content_filter" if mode == "blocked" else "stop",
            ),
        )

    settings = replace(
        Settings(), provider="groq", model="selected-model", database=str(tmp_path / "test.db")
    )
    with TestClient(create_app(settings, httpx.MockTransport(handler))) as client:
        cid = client.post("/conversations", json={}).json()["id"]
        result = client.post(f"/conversations/{cid}/messages", json={"message": "hello"})
        assert result.status_code == status
        assert (
            "test-secret" not in result.text + caplog.text
            and "prompt-data" not in result.text + caplog.text
        )
        assert client.get(f"/conversations/{cid}/messages").json()["messages"] == []
        assert len(calls) == 1  # No redirects or hidden billable retries.


def test_credentials_files_missing_conflict_and_runtime_config(tmp_path, monkeypatch):
    file = tmp_path / "secret"
    file.write_text("mounted-secret\n")
    monkeypatch.setenv("GOOGLE_API_KEY_FILE", str(file))
    with pytest.raises(ValueError, match="only one"):
        credential("google")
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert credential("google") == "mounted-secret"
    file.unlink()
    with pytest.raises(ValueError, match="Cannot read"):
        credential("google")
    monkeypatch.delenv("GOOGLE_API_KEY_FILE")
    with pytest.raises(ValueError, match="Set GOOGLE"):
        credential("google")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    assert Settings().provider == "groq" and Settings().model == "my-model"
    monkeypatch.setenv("LLM_MODEL", "")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        create_app()
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        create_app()


def test_google_embeddings_tasks_batching_normalization_and_identity(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "google")
    settings = replace(EmbeddingSettings.from_env(), batch_size=2)
    calls = []

    def handler(request):
        assert (
            request.url
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents"
        )
        assert request.headers["x-goog-api-key"] == "test-secret"
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200, json={"embeddings": [{"values": [2.0] + [0.0] * 767} for _ in body["requests"]]}
        )

    with create_embedder(settings, httpx.MockTransport(handler)) as engine:
        assert len(engine.embed(["a", "b", "c"])) == 3
        assert query_vector(engine, "query")[0] == 1
        digest = engine.verify_identity()
        with pytest.raises(CatalogError, match="rebuild"):
            engine.verify_identity("a" * 64)
    assert [len(body["requests"]) for body in calls] == [2, 1, 1]
    assert calls[0]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    assert calls[-1]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert calls[-1]["requests"][0]["content"]["parts"][0]["text"] == "query"
    assert all(item["outputDimensionality"] == 768 for body in calls for item in body["requests"])
    with create_embedder(replace(settings, revision="2"), httpx.MockTransport(handler)) as engine:
        assert digest != engine.digest


@pytest.mark.parametrize(
    "payload",
    [
        {"embeddings": []},
        {"embeddings": [{"values": [0.0] * 768}]},
        {"embeddings": [{"values": [1.0] * 767}]},
        {"embeddings": None},
    ],
)
def test_google_invalid_vectors_fail_closed(monkeypatch, payload):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "google")
    with create_embedder(
        EmbeddingSettings.from_env(),
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as engine:
        with pytest.raises(CatalogError):
            engine.embed(["text"])


def test_voyage_rank_preserves_indexes_and_disables_truncation(monkeypatch):
    monkeypatch.setenv("RERANK_PROVIDER", "voyage")
    assert isinstance(create_reranker(), VoyageReranker)

    def handler(request):
        assert request.url == "https://api.voyageai.com/v1/rerank"
        assert request.headers["authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert (
            body["truncation"] is False and body["return_documents"] is False and body["top_k"] == 2
        )
        return httpx.Response(
            200,
            json={
                "data": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
            },
        )

    result = VoyageReranker(httpx.MockTransport(handler)).rank(
        "q", ["a", "b"], 10, preserve_query=True
    )
    assert result["results"] == [
        {"index": 1, "score": 0.9, "text": "b"},
        {"index": 0, "score": 0.1, "text": "a"},
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": 4, "relevance_score": 0.1}],
        [{"index": True, "relevance_score": 0.1}],
        [{"index": 0, "relevance_score": "0.1"}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
    ],
)
def test_voyage_rejects_malformed_rankings(rows):
    engine = VoyageReranker(httpx.MockTransport(lambda _: httpx.Response(200, json={"data": rows})))
    with pytest.raises(RuntimeError, match="Voyage"):
        engine.rank("q", ["a", "b"], min(len(rows), 2) or 1)


@pytest.mark.parametrize("provider", ["google", "groq", "openrouter"])
def test_health_uses_selected_hosted_provider(tmp_path, provider):
    def handler(request):
        assert request.url.path.endswith("/models") and request.url.scheme == "https"
        return httpx.Response(200, json={"data": [{"id": "selected-model"}]})

    settings = replace(
        Settings(), provider=provider, model="selected-model", database=str(tmp_path / "test.db")
    )
    with TestClient(create_app(settings, httpx.MockTransport(handler))) as client:
        result = client.get("/health")
        assert result.status_code == 200 and result.json()["provider"] == provider


def test_hosted_dependencies_do_not_require_local_models(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import dependencies
    from app.operations import Metrics

    monkeypatch.setenv("EMBEDDING_PROVIDER", "google")
    settings = EmbeddingSettings.from_env()
    with create_embedder(settings) as embedder:
        digest = embedder.digest

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(dependencies.psycopg, "connect", lambda *a, **k: Database())
    monkeypatch.setattr(
        "app.product_search.pin_search_build",
        lambda db: {
            "build_id": "google-build",
            "config": {"model": settings.model, "model_digest": digest},
        },
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host in {"api.groq.com", "generativelanguage.googleapis.com"}
        if request.url.host == "api.groq.com":
            return httpx.Response(200, json={"data": [{"id": "selected-model"}]})
        return httpx.Response(200, json={"name": "models/" + settings.model})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )

    async def probe():
        async with httpx.AsyncClient(base_url="https://api.groq.com/openai/v1/") as client:
            state = SimpleNamespace(
                store=Store(tmp_path / "test.db"),
                metrics=Metrics(),
                ollama=client,
                llm_provider="groq",
                reranker=VoyageReranker(),
                product_search=SimpleNamespace(
                    settings=settings, database_url="fixture", cache=SimpleNamespace(client=None)
                ),
            )
            return await dependencies.probe_dependencies(state, "selected-model")

    result = asyncio.run(probe())
    assert all(
        result[name]["status"] == "ready" for name in ("models", "postgres", "reranker", "sqlite")
    )
    assert result["reranker"]["check"] == "configuration_only"
    assert result["reranker"]["inference_verified"] is False
    assert len(calls) == 2


def test_provider_configuration_rejects_bad_limits_and_unselected_keys_are_optional(monkeypatch):
    for name in KEY_NAMES.values():
        monkeypatch.delenv(name)
    assert EmbeddingSettings.from_env().provider == "ollama"
    assert create_reranker().__class__.__name__ == "LocalReranker"
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bad")
    with pytest.raises(CatalogError):
        EmbeddingSettings.from_env()
    monkeypatch.setenv("RERANK_PROVIDER", "bad")
    with pytest.raises(ValueError):
        create_reranker()
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ValueError, match="finite"):
        create_app()


@pytest.mark.parametrize("provider", ["google", "voyage"])
def test_sync_provider_errors_do_not_expose_response_bodies(monkeypatch, provider):
    def handler(request):
        return httpx.Response(401, text="test-secret private-query")

    if provider == "google":
        monkeypatch.setenv("EMBEDDING_PROVIDER", "google")
        with create_embedder(EmbeddingSettings.from_env(), httpx.MockTransport(handler)) as engine:
            with pytest.raises(CatalogError) as error:
                engine.embed(["private-query"])
    else:
        with pytest.raises(RuntimeError) as error:
            VoyageReranker(httpx.MockTransport(handler)).rank("private-query", ["doc"], 1)
    assert "test-secret" not in str(error.value) and "private-query" not in str(error.value)
    assert error.value.__suppress_context__

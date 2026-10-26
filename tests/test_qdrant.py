"""Qdrant protocol, scope integrity, resumability, and optional real-service checks."""

import asyncio
import copy
import json
import os
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from test_catalog import make_source, product
from test_search import FakeEmbedder
from test_search_cache import MemoryClient

from app.catalog import CatalogError, connect_database, import_catalog, validated_source
from app.dependencies import probe_dependencies
from app.product_search import ProductSearch, ProductSearchRequest, pin_search_build, search_catalog
from app.qdrant_client import (
    CONTRACT_SHA,
    QdrantClient,
    QdrantSettings,
    point_id,
)
from app.qdrant_index import pin_qdrant_index, sync_qdrant_index, verify_qdrant_index
from app.search_cache import SearchCache
from app.search_index import build_index
from app.search_maintenance import maintenance_lock
from app.search_normalization import normalize_search


class MemoryService:
    """Transport-level fake: assertions below inspect real client request bodies."""

    def __init__(self):
        self.collections = {}
        self.calls = []
        self.upserts = 0
        self.fail_upsert = None
        self.corrupt_hit = None

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        parts = request.url.path.strip("/").split("/")
        collection = parts[1]
        method = request.method
        if len(parts) == 2 and method == "PUT":
            self.collections[collection] = {"config": {"params": body}, "points": {}}
            result = True
        elif collection not in self.collections:
            return httpx.Response(404, json={"status": {"error": "missing"}})
        elif len(parts) == 2:
            result = {"status": "green", "config": self.collections[collection]["config"]}
        elif parts[2] == "index":
            result = {"status": "completed"}
        elif parts[-1] == "count":
            result = {"count": len(self.collections[collection]["points"])}
        elif parts[-1] == "batch":
            result = []
            for query in body["searches"]:
                allowed = query["filter"]["must"][0]["has_id"]
                hits = [
                    {"id": row["id"], "payload": copy.deepcopy(row["payload"]), "score": 0.9}
                    for row in self.collections[collection]["points"].values()
                    if row["id"] in allowed
                ][: query["limit"]]
                if self.corrupt_hit and hits:
                    self.corrupt_hit(hits[0])
                result.append({"points": hits})
        elif method == "PUT":
            self.upserts += 1
            if self.upserts == self.fail_upsert:
                return httpx.Response(503, json={"error": "injected failure"})
            for point in copy.deepcopy(body["points"]):
                point["vector"]["lexical"] = {"indices": [1], "values": [1.0]}
                self.collections[collection]["points"][point["id"]] = point
            result = {"status": "completed"}
        else:
            result = [
                copy.deepcopy(self.collections[collection]["points"][key])
                for key in body["ids"]
                if key in self.collections[collection]["points"]
            ]
        return httpx.Response(200, json={"status": "ok", "result": result})


@pytest.fixture
def service():
    return MemoryService()


@pytest.fixture
def client(service):
    with QdrantClient(QdrantSettings(), httpx.MockTransport(service)) as instance:
        yield instance


def seed(pg, url, tmp_path, records):
    make_source(tmp_path, records)
    with validated_source(tmp_path) as source:
        import_catalog(pg, source)
    pg.commit()
    # Protocol tests create vectors first, then exercise export/failure explicitly.
    with connect_database(url, autocommit=True) as db, patch("app.qdrant_index.sync_qdrant_index"):
        return build_index(db, FakeEmbedder(batch_size=128), lambda *a, **kw: None)["build_id"]


def export(url, client, build_id):
    with connect_database(url, autocommit=True) as db, maintenance_lock(db):
        return sync_qdrant_index(db, client, build_id, lambda *a, **kw: None)


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://remote.example:6333"},
        {"url": "https://user:secret@example.com"},
        {"url": "https://example.com/?api-key=secret"},
        {"prefix": "../bad"},
        {"timeout": float("nan")},
        {"timeout": 121},
        {"max_eligible": 100001},
    ],
)
def test_invalid_settings(changes):
    with pytest.raises(CatalogError):
        QdrantSettings(**changes)


def test_configuration_defaults_and_identity(monkeypatch):
    assert QdrantSettings.from_env().url.startswith("http")
    assert "secret" not in repr(QdrantSettings(api_key="secret"))
    assert point_id("amazon:B000000001") == point_id("amazon:B000000001")
    assert QdrantSettings().collection("a" * 64) != QdrantSettings().collection("b" * 64)
    monkeypatch.setenv("SEARCH_BACKEND", "typo")
    with pytest.raises(CatalogError):
        QdrantSettings.from_env()
    monkeypatch.delenv("SEARCH_BACKEND")
    monkeypatch.setenv("QDRANT_EXACT", "true")
    with pytest.raises(CatalogError):
        QdrantSettings.from_env()


def test_readiness_reports_qdrant_failure_separately_from_postgres(monkeypatch):
    class Database:
        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return (1,)

    async def probe_llm(*args):
        return {"provider": "test"}

    def unavailable(*args):
        raise CatalogError("private provider failure text")

    monkeypatch.setattr("psycopg.connect", lambda *a, **kw: nullcontext(Database()))
    monkeypatch.setattr(
        "app.product_search.pin_search_build",
        lambda *a: {"build_id": "a" * 64, "config": {"model": "embeddinggemma:latest"}},
    )
    monkeypatch.setattr("app.embeddings.create_embedder", lambda *a: nullcontext(FakeEmbedder()))
    monkeypatch.setattr("app.providers.probe_llm", probe_llm)
    monkeypatch.setattr("app.providers.credential", lambda *a: "test")
    monkeypatch.setattr("app.qdrant_client.QdrantClient", lambda *a: nullcontext(object()))
    monkeypatch.setattr("app.qdrant_index.pin_qdrant_index", unavailable)
    state = SimpleNamespace(
        store=SimpleNamespace(connect=lambda: nullcontext(Database())),
        product_search=SimpleNamespace(
            database_url="",
            settings=FakeEmbedder().settings,
            cache=SimpleNamespace(client=None),
            qdrant_settings=QdrantSettings(),
        ),
        ollama=None,
        reranker=SimpleNamespace(provider="voyage"),
        metrics=SimpleNamespace(),
    )
    result = asyncio.run(probe_dependencies(state, "test"))
    assert result["postgres"]["status"] == "ready"
    assert result["qdrant"]["status"] == "unavailable"
    assert result["qdrant"]["error_type"] == "CatalogError"
    assert "private provider failure" not in json.dumps(result)


def test_transport_failures_are_sanitized():
    def handler(request):
        raise httpx.ConnectError("api-key=super-secret", request=request)

    with QdrantClient(
        QdrantSettings(api_key="super-secret"), httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(CatalogError) as error:
            client.info("collection")
        assert "super-secret" not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda hit: hit.update(id=point_id("amazon:B000000002")),
        lambda hit: hit["payload"].update(product_id="amazon:B000000002"),
        lambda hit: hit["payload"].update(contract_sha="bad"),
        lambda hit: hit.update(score="0.9"),
    ],
)
def test_invalid_candidate_identity_is_rejected(client, service, mutation):
    client.create("demo")
    pinned = {"build_id": "a" * 64, "import_id": "b" * 64}
    key = "amazon:B000000001"
    client.upsert(
        "demo",
        [
            {
                "id": point_id(key),
                "vector": {"dense": [1.0], "lexical": {}},
                "payload": {**pinned, "product_id": key, "contract_sha": CONTRACT_SHA},
            }
        ],
    )
    service.corrupt_hit = mutation
    with pytest.raises(CatalogError, match="out-of-scope"):
        client.branches("demo", pinned, ProductSearchRequest(query="test"), [1.0], [key])


def test_hybrid_requests_share_authoritative_filters(client, service):
    request = ProductSearchRequest(query="shampoo", filters={"storefront_id": "beauty"})
    pinned = {"build_id": "a" * 64, "import_id": "b" * 64}
    client.create("demo")
    key = "amazon:B000000001"
    client.upsert(
        "demo",
        [
            {
                "id": point_id(key),
                "vector": {"dense": [1.0], "lexical": {}},
                "payload": {**pinned, "contract_sha": CONTRACT_SHA, "product_id": key},
            }
        ],
    )
    keyword, semantic = client.branches("demo", pinned, request, [1.0], [key])
    assert keyword == [(key, 0.9)]
    assert semantic[0][1] == pytest.approx(0.1)
    searches = service.calls[-1][2]["searches"]
    assert searches[0]["filter"] == searches[1]["filter"]
    assert searches[0]["query"]["model"] == "qdrant/bm25"
    assert searches[1]["params"]["exact"] is True
    assert {"key": "storefront_id", "match": {"value": "beauty"}} in searches[0]["filter"]["must"]
    assert searches[0]["filter"]["must"][0] == {"has_id": [point_id(key)]}
    service.corrupt_hit = lambda hit: hit["payload"].update(import_id="wrong")
    with pytest.raises(CatalogError, match="out-of-scope"):
        client.branches("demo", pinned, request, [1.0], [key])


def test_export_scope_and_authoritative_hydration(pg, scratch_database, tmp_path, client, service):
    rows = [product(), product("B000000002", "100.00"), product("B000000003")]
    rows[2]["category"].update(domain="fashion", family="apparel")
    rows[2]["title"] = "Cotton shirt"
    build_id = seed(pg, scratch_database, tmp_path, rows)
    result = export(scratch_database, client, build_id)
    assert result["products"] == 3 and result["embedding_calls"] == 0
    assert export(scratch_database, client, build_id)["status"] == "reused"
    request = ProductSearchRequest(
        query="moisturizer",
        product_goal="moisturizer",
        filters={"storefront_id": "beauty", "max_price": "30"},
    )
    response = search_catalog(pg, request, FakeEmbedder(), qdrant=client)
    assert response["eligible_products"] == 1
    assert [row["product_id"] for row in response["results"]] == ["amazon:B000000001"]
    assert response["results"][0]["title"] == rows[0]["title"]
    assert response["method"] == "qdrant_bm25+exact_cosine+rrf"
    empty = ProductSearchRequest(query="moisturizer", filters={"max_price": "0.01"})
    assert search_catalog(pg, empty, FakeEmbedder(fail_after=0), qdrant=client)["results"] == []
    # Dropped collection points must not be hidden by a ready PostgreSQL registry row.
    service.collections[result["collection"]]["points"].pop(point_id("amazon:B000000001"))
    with pytest.raises(CatalogError, match="count differs"):
        pin_qdrant_index(pg, pin_search_build(pg), client)


def test_partial_export_resumes_without_publishing(pg, scratch_database, tmp_path, client, service):
    rows = [product(f"B{i:09d}") for i in range(130)]
    build_id = seed(pg, scratch_database, tmp_path, rows)
    service.fail_upsert = 2
    with pytest.raises(CatalogError):
        export(scratch_database, client, build_id)
    status, last = pg.execute(
        "SELECT status,last_product_id FROM commerce.qdrant_indexes"
    ).fetchone()
    assert status == "building" and last == rows[127]["product_id"]
    with pytest.raises(CatalogError, match="not ready"):
        pin_qdrant_index(pg, pin_search_build(pg), client)
    pg.commit()
    service.fail_upsert = None
    assert export(scratch_database, client, build_id)["products"] == 130
    assert service.upserts == 3
    # Published data is never repaired in place, even when payload/vector verification fails.
    collection = client.settings.collection(build_id)
    service.collections[collection]["points"][point_id(rows[0]["product_id"])]["payload"][
        "source_sha"
    ] = "bad"
    with pytest.raises(CatalogError, match="contents differ"):
        export(scratch_database, client, build_id)
    assert service.upserts == 3


def test_failed_qdrant_publication_keeps_old_generation(
    pg, scratch_database, tmp_path, monkeypatch
):
    old = seed(pg, scratch_database, tmp_path, [product()])
    changed = product()
    changed["title"] = "New moisturizer listing"
    make_source(tmp_path, [changed])
    with validated_source(tmp_path) as source:
        staged = import_catalog(pg, source, activate=False)["import_id"]
    pg.commit()

    def fail(*a, **kw):
        raise CatalogError("Injected export failure")

    monkeypatch.setattr("app.qdrant_index.sync_qdrant_index", fail)
    with connect_database(scratch_database, autocommit=True) as db:
        with pytest.raises(CatalogError, match="Injected"):
            build_index(db, FakeEmbedder(), lambda *a, **kw: None, catalog_import_id=staged)
        assert pin_search_build(db)["build_id"] == old


def test_complete_qdrant_generation_publishes_with_catalog(
    pg, scratch_database, tmp_path, monkeypatch, client, service
):
    old = seed(pg, scratch_database, tmp_path, [product()])
    changed = product("B000000002")
    changed["title"] = "New moisturizer listing"
    make_source(tmp_path, [changed])
    with validated_source(tmp_path) as source:
        staged = import_catalog(pg, source, activate=False)["import_id"]
    pg.commit()
    monkeypatch.setattr("app.qdrant_client.QdrantClient", lambda *a, **kw: client)
    with connect_database(scratch_database, autocommit=True) as db:
        result = build_index(db, FakeEmbedder(), lambda *a, **kw: None, catalog_import_id=staged)
        pinned = pin_search_build(db)
        assert pinned["build_id"] == result["build_id"] != old
        assert pinned["import_id"] == staged
        registered = db.execute(
            "SELECT status,verified_count FROM commerce.qdrant_indexes"
        ).fetchone()
        assert registered == ("ready", 1)
        points = service.collections[client.settings.collection(result["build_id"])]["points"]
        assert {row["payload"]["product_id"] for row in points.values()} == {changed["product_id"]}


def test_eligible_cap_never_silently_truncates(pg, scratch_database, tmp_path, client):
    build_id = seed(pg, scratch_database, tmp_path, [product(), product("B000000002")])
    export(scratch_database, client, build_id)
    client.settings = replace(client.settings, max_eligible=1)
    with pytest.raises(CatalogError, match="eligibility limit"):
        search_catalog(
            pg, ProductSearchRequest(query="moisturizer"), FakeEmbedder(fail_after=0), qdrant=client
        )


def test_empty_catalog_publishes_empty_qdrant_generation(pg, scratch_database, tmp_path, client):
    build_id = seed(pg, scratch_database, tmp_path, [])
    result = export(scratch_database, client, build_id)
    assert result["products"] == 0
    assert (
        search_catalog(
            pg, ProductSearchRequest(query="moisturizer"), FakeEmbedder(fail_after=0), qdrant=client
        )["results"]
        == []
    )


def test_serving_cache_requires_ready_collection_and_rejects_retired_backend(
    pg, scratch_database, tmp_path, client, service, monkeypatch
):
    build_id = seed(pg, scratch_database, tmp_path, [product()])
    export(scratch_database, client, build_id)
    monkeypatch.setattr("app.product_search.QdrantClient", lambda *a: nullcontext(client))
    monkeypatch.setattr(
        "app.product_search.create_embedder", lambda *a: nullcontext(FakeEmbedder())
    )
    cache = SearchCache(client=MemoryClient())
    search = ProductSearch(scratch_database, reranker=object(), cache=cache)
    request = ProductSearchRequest(query="moisturizer", rerank=False)
    result = search.search(request)
    assert result["method"] == "qdrant_bm25+exact_cosine+rrf"
    assert result["results"][0]["product_id"] == "amazon:B000000001"
    assert search.search(request)["cache"]["status"] == "hit"
    service.collections[client.settings.collection(build_id)]["points"].clear()
    with pytest.raises(CatalogError, match="count differs"):
        search.search(request)
    monkeypatch.setenv("SEARCH_BACKEND", "postgres")
    with pytest.raises(CatalogError, match="retired"):
        ProductSearch(scratch_database, reranker=object(), cache=cache)


def test_typo_preference_normalizes_search_without_changing_saved_evidence(
    pg, scratch_database, tmp_path, client, service
):
    build_id = seed(pg, scratch_database, tmp_path, [product()])
    export(scratch_database, client, build_id)
    request = ProductSearchRequest(
        query="skin care lightens my face; kess chemincal", concerns=["kess chemincal"]
    )
    original = request.model_dump()
    response = search_catalog(pg, request, FakeEmbedder(), qdrant=client)
    query_call = next(
        body for method, path, body in reversed(service.calls) if path.endswith("/query/batch")
    )
    assert query_call["searches"][0]["query"]["text"] == "skin care lightens my face; less chemical"
    assert response["query"] == original["query"]
    assert request.model_dump() == original
    assert normalize_search("Kess necklace and Kessa glove") == "Kess necklace and Kessa glove"


def test_real_qdrant_export_and_filtered_hybrid(pg, scratch_database, tmp_path):
    url = os.getenv("QDRANT_TEST_URL")
    if not url:
        pytest.skip("Set QDRANT_TEST_URL for an isolated collection on a running Qdrant service")
    settings = replace(QdrantSettings(), url=url, prefix="test_" + uuid4().hex[:16])
    build_id = seed(pg, scratch_database, tmp_path, [product(), product("B000000002", "100.00")])
    collection = settings.collection(build_id)
    with QdrantClient(settings) as client:
        try:
            result = export(scratch_database, client, build_id)
            assert result["products"] == 2
            assert verify_qdrant_index(pg, client, build_id)["products"] == 2
            response = search_catalog(
                pg,
                ProductSearchRequest(
                    query="moisturizer", filters={"storefront_id": "beauty", "max_price": "30"}
                ),
                FakeEmbedder(),
                qdrant=client,
            )
            assert [row["product_id"] for row in response["results"]] == ["amazon:B000000001"]
            assert response["results"][0]["ranking"]["keyword_rank"] == 1
        finally:
            client.request("DELETE", f"/collections/{collection}", missing_ok=True)

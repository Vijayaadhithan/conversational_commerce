"""Search timings describe current work even when cached payloads contain old durations."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

from app import product_search
from app.search_models import ProductSearchRequest


def test_cached_search_reports_no_embedding_or_reranking_work(monkeypatch):
    request = ProductSearchRequest(query="moisturizer")
    payload = {
        "build_id": "ready",
        "query": request.query,
        "filters_applied": request.filters.model_dump(mode="json", exclude_none=True),
        "results": [],
        "timings": {"embedding_seconds": 99, "reranking_seconds": 100},
    }
    cache = SimpleNamespace(key=lambda *args: "key", get=lambda _: (deepcopy(payload), "hit"))
    db, client, embedder = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(product_search, "connect_database", lambda _: db)
    monkeypatch.setattr(product_search, "QdrantClient", lambda _: client)
    monkeypatch.setattr(product_search, "create_embedder", lambda _: embedder)
    monkeypatch.setattr(
        product_search,
        "pin_search_build",
        lambda _: {"build_id": "ready", "config": {"model": "fixture", "model_digest": "fixture"}},
    )
    monkeypatch.setattr(product_search, "pin_qdrant_index", lambda *args: "ready_collection")
    reranker = MagicMock()
    search = product_search.ProductSearch(
        settings=SimpleNamespace(model="fixture"), reranker=reranker, cache=cache
    )
    result = search.search(request)
    assert result["cache"]["status"] == "hit"
    assert result["timings"]["embedding_seconds"] == 0
    assert result["timings"]["reranking_seconds"] == 0
    assert result["timings"]["total_seconds"] >= result["timings"]["setup_cache_seconds"] >= 0
    reranker.rank.assert_not_called()
    assert payload["timings"]["embedding_seconds"] == 99


def test_empty_eligibility_does_not_measure_embedding_or_query_qdrant():
    db, qdrant, embedder = MagicMock(), MagicMock(), MagicMock()
    db.execute.return_value.fetchall.return_value = []
    qdrant.settings.max_eligible = 100
    qdrant.settings.exact = True
    embedder.settings.model = "fixture"
    phases = {}
    result = product_search.search_catalog(
        db,
        ProductSearchRequest(query="moisturizer"),
        embedder,
        {
            "import_id": "catalog",
            "run_id": "run",
            "build_id": "ready",
            "config": {"model": "fixture", "model_digest": "fixture"},
        },
        qdrant=qdrant,
        collection="ready_collection",
        phase_timings=phases,
    )
    assert result["results"] == [] and set(phases) == {"eligibility_seconds"}
    qdrant.branches.assert_not_called()
    embedder.verify_identity.assert_not_called()

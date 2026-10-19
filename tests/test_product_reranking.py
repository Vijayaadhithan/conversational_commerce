"""Reranking may reorder only the candidates already admitted by SQL filters."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import product_search
from app.catalog import CatalogError, connect_database, import_catalog, validated_source
from app.product_reranking import rerank_candidates, rerank_document
from app.product_search import ProductSearch, ProductSearchRequest
from app.reranker import LocalReranker
from app.search_index import build_index
from test_catalog import make_source, pg, product, scratch_database
from test_search import FakeEmbedder
from app.search_cache import SearchCache
from test_search_cache import MemoryClient


def response():
    return {"method": "postgres_fts+exact_cosine+rrf", "results": [
        {"product_id": "a", "ranking": {"rrf_score": 0.02}},
        {"product_id": "b", "ranking": {"rrf_score": 0.01}},
    ]}


class Ranker:
    def rank(self, query, documents, top_k, **kwargs):
        assert kwargs["preserve_query"] is True and top_k == len(documents)
        return {"model": "test", "device": "cpu", "results": [
            {"index": index, "score": float(index), "text": text} for index, text in enumerate(documents)]}


def test_rerank_maps_scores_to_original_ids_and_keeps_retrieval_signals():
    result = rerank_candidates(response(), {"a": "first", "b": "second"}, ProductSearchRequest(query="test", limit=1), Ranker())
    assert [row["product_id"] for row in result["results"]] == ["b"]
    assert result["results"][0]["ranking"] == {"rrf_score": 0.01, "rrf_rank": 2, "rerank_rank": 1, "rerank_score": 1.0}
    assert result["reranking"]["applied"] is True


@pytest.mark.parametrize("bad", [
    [], [{"index": 0, "score": 1.0}] * 2,
    [{"index": 0, "score": 1.0}, {"index": 2, "score": 1.0}],
    [{"index": 0, "score": 1.0}, {"index": 1, "score": float('nan')}],
    [{"index": 0, "score": 1.0}, {"index": True, "score": 1.0}],
    [{"index": 0, "score": 1.0}, {"index": 1, "score": 1.0, "text": "invented"}],
])
def test_incomplete_duplicate_invented_and_nonfinite_results_rejected(bad):
    class BadRanker:
        def rank(self, *args, **kwargs): return {"results": bad}
    with pytest.raises(CatalogError, match="unavailable or invalid"):
        rerank_candidates(response(), {"a": "first", "b": "second"}, ProductSearchRequest(query="test"), BadRanker())


def test_equal_scores_preserve_rrf_order():
    class TieRanker:
        def rank(self, *args, **kwargs): return {"results": [{"index": 1, "score": 1.0}, {"index": 0, "score": 1.0}]}
    result = rerank_candidates(response(), {"a": "first", "b": "second"}, ProductSearchRequest(query="test"), TieRanker())
    assert [row["product_id"] for row in result["results"]] == ["a", "b"]


def test_document_selection_keeps_title_first_and_source_unchanged():
    record = product()
    record["content"]["description"] = ["💄" * 3000]
    original = deepcopy(record)
    text = rerank_document(record)
    assert text.startswith("Title: Example moisturizer") and len(text.encode()) <= 4000
    assert record == original


def test_product_query_budget_expands_pair_window_without_changing_raw_rerank():
    class Model:
        tokenizer = SimpleNamespace(truncation_side="right")
        def predict(self, pairs, **kwargs):
            self.kwargs = kwargs
            return [1.0] * len(pairs)
    engine = LocalReranker()
    engine.model = Model()
    assert engine.rank("💄" * 250, ["listing"], 1, preserve_query=True)["max_pair_tokens"] >= 1256
    assert engine.model.kwargs["processing_kwargs"]["text"]["max_length"] >= 1256
    engine.rank("ordinary query", ["listing"], 1)
    assert "processing_kwargs" not in engine.model.kwargs


def test_real_sql_filters_still_bound_reranker_and_disabled_mode_skips_model(pg, scratch_database, tmp_path, monkeypatch):
    rows = [product(), product("B000000002"), product("B000000003", "100.00")]
    for row in rows[1:]: row["attributes"]["fragrance_free"] = {"value": True, "field": "/details/Scent", "status": "listing_claim"}
    make_source(tmp_path, rows)
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        build_index(db, FakeEmbedder(), lambda *args, **kwargs: None)
    class ContextEmbedder(FakeEmbedder):
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(product_search, "OllamaEmbedder", lambda settings: ContextEmbedder())
    class BoundRanker(Ranker):
        def rank(self, query, documents, top_k, **kwargs):
            assert len(documents) == 1  # Neither the unknown claim nor over-budget product can enter.
            return super().rank(query, documents, top_k, **kwargs)
    service = ProductSearch(scratch_database, reranker=BoundRanker())
    request = ProductSearchRequest(query="moisturizer", filters={"max_price": "30.00", "attributes": {"fragrance_free": True}})
    result = service.search(request)
    assert [row["product_id"] for row in result["results"]] == ["amazon:B000000002"]
    service.reranker = Ranker()
    one = ProductSearchRequest(query="moisturizer", filters={"max_price": "30.00"}, limit=1)
    assert service.search(one)["results"][0]["product_id"] == "amazon:B000000002"
    assert service.search(one.model_copy(update={"rerank": False}))["results"][0]["product_id"] == "amazon:B000000001"
    class BrokenRanker:
        def rank(self, *args, **kwargs): raise RuntimeError("Model missing")
    service.reranker = BrokenRanker()
    with pytest.raises(CatalogError): service.search(request)
    assert service.search(request.model_copy(update={"rerank": False}))["reranking"]["reason"] == "disabled"
    empty = ProductSearchRequest(query="moisturizer", filters={"max_price": "0.00"})
    assert service.search(empty)["reranking"]["reason"] == "no_candidates"


def test_cache_hits_do_not_retrieve_and_new_generation_or_filter_cannot_reuse_old_results(pg, scratch_database, tmp_path, monkeypatch):
    rows = [product(), product("B000000002", "25.00")]
    make_source(tmp_path, rows)
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        build_index(db, FakeEmbedder(), lambda *a, **kw: None)
    class ContextEmbedder(FakeEmbedder):
        queries = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def embed(self, texts):
            type(self).queries += 1
            return super().embed(texts)
    monkeypatch.setattr(product_search, "OllamaEmbedder", lambda settings: ContextEmbedder())
    service = ProductSearch(scratch_database, reranker=Ranker(), cache=SearchCache(client=MemoryClient()))
    request = ProductSearchRequest(query="moisturizer", rerank=False)
    first = service.search(request)
    assert service.search(request)["cache"]["status"] == "hit" and ContextEmbedder.queries == 1
    constrained = service.search(ProductSearchRequest(query="moisturizer", rerank=False, filters={"max_price": "20.00"}))
    assert len(constrained["results"]) == 1 and ContextEmbedder.queries == 2
    make_source(tmp_path, [rows[1]])
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        build_index(db, FakeEmbedder(), lambda *a, **kw: None)
    updated = service.search(request)
    assert updated["build_id"] != first["build_id"] and updated["cache"]["status"] == "miss"
    assert [row["product_id"] for row in updated["results"]] == [rows[1]["product_id"]]

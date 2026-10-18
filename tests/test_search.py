"""Model protocol and PostgreSQL search generation behavior without model weights."""

import json

import httpx
import pytest

from app.catalog import CatalogError, connect_database, import_catalog, validated_source
from app.embedding_text import DOCUMENT_BYTES, document_text
from app.embeddings import DIMENSIONS, EmbeddingSettings, OllamaEmbedder, validate_vectors
from app.search_index import build_index, verify_build
from app.product_search import ProductFilters, ProductSearchRequest, pin_search_build, reciprocal_rank_fusion, search_catalog
from test_catalog import make_source, pg, product, scratch_database


class FakeEmbedder:
    def __init__(self, batch_size=1, fail_after=None):
        self.settings = EmbeddingSettings(batch_size=batch_size)
        self.digest = "a" * 64
        self.calls = 0
        self.fail_after = fail_after

    def verify_identity(self, expected=None):
        if expected and self.digest != expected:
            raise CatalogError("Digest changed")
        return self.digest

    def embed(self, texts):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise CatalogError("Injected model failure")
        self.calls += 1
        return [[1.0] + [0.0] * (DIMENSIONS - 1) for _ in texts]


def test_document_contract_bounds_unicode_and_preserves_original():
    record = product()
    record["content"]["description"] = ["💄" * 5000]
    result = document_text(record)
    assert len(result["input"].encode()) <= DOCUMENT_BYTES
    assert result["input"].startswith("title: Example moisturizer | text: ")
    assert result["clipped"] is True
    assert len(record["content"]["description"][0]) == 5000
    assert "fragrance free" not in result["input"]


@pytest.mark.parametrize("vectors", [None, [], [[0.0] * 768], [[1.0] * 767], [[float('nan')] * 768], [[True] * 768]])
def test_invalid_vectors_rejected(vectors):
    with pytest.raises(CatalogError): validate_vectors(vectors, 1)


def test_embed_protocol_checks_digest_and_disables_silent_truncation():
    seen = []
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "embeddinggemma:latest", "digest": "a" * 64}]})
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"embeddings": [[2.0] + [0.0] * 767]})
    with OllamaEmbedder(EmbeddingSettings(), httpx.MockTransport(handler)) as model:
        assert model.embed(["test"])[0][0] == 1.0
        with pytest.raises(CatalogError, match="digest changed"):
            model.verify_identity("b" * 64)
    assert seen[0]["truncate"] is False


def test_build_resumes_failed_batch_and_reuses_ready_generation(pg, scratch_database, tmp_path):
    make_source(tmp_path, [product(), product("B000000002")])
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    quiet = lambda *args, **kwargs: None
    with connect_database(scratch_database, autocommit=True) as db:
        with pytest.raises(CatalogError, match="Injected"):
            build_index(db, FakeEmbedder(fail_after=1), quiet)
        assert db.execute("SELECT count(*) FROM commerce.search_documents").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM commerce.search_active").fetchone() == (0,)
        model = FakeEmbedder()
        result = build_index(db, model, quiet)
        assert result["products"] == 2 and model.calls == 1
        model = FakeEmbedder(fail_after=0)
        assert build_index(db, model, quiet)["status"] == "reused"
        with db.transaction(): assert verify_build(db, result["build_id"])["products"] == 2


def test_index_verification_detects_changed_catalog(pg, scratch_database, tmp_path):
    make_source(tmp_path)
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        build = build_index(db, FakeEmbedder(), lambda *args, **kwargs: None)
        db.execute("UPDATE commerce.catalog_products SET document=jsonb_set(document,'{title}','\"Changed title\"')")
        with db.transaction(), pytest.raises(CatalogError, match="differs"):
            verify_build(db, build["build_id"])


def test_rank_fusion_combines_independent_ranks_deterministically():
    fused = reciprocal_rank_fusion([("b", 10), ("a", 5)], [("a", 0.1), ("c", 0.2)])
    assert [item[0] for item in fused] == ["a", "b", "c"]
    assert fused[0][1]["keyword_rank"] == 2 and fused[0][1]["vector_rank"] == 1


def test_filtered_search_excludes_unknowns_wrong_categories_and_over_budget(pg, scratch_database, tmp_path):
    rows = [product(), product("B000000002"), product("B000000003", "100.00"), product("B000000004", "8.00")]
    rows[1]["title"] = "Fragrance free moisturizer"
    rows[1]["brand"] = {"value": "O'Reilly", "field": "/details/Brand", "status": "listing_claim"}
    for row in rows[1:3]:
        row["attributes"]["fragrance_free"] = {"value": True, "field": "/details/Scent", "status": "listing_claim"}
    rows[3]["title"] = "Cotton shirt"
    rows[3]["category"].update(domain="fashion", family="apparel")
    make_source(tmp_path, rows)
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        build_index(db, FakeEmbedder(), lambda *args, **kwargs: None)
    filters = {"domain": "beauty", "max_price": "30.00", "attributes": {"fragrance_free": True}}
    result = search_catalog(pg, ProductSearchRequest(query="moisturizer", filters=filters), FakeEmbedder())
    assert [row["product_id"] for row in result["results"]] == ["amazon:B000000002"]
    assert result["eligible_products"] == 1
    assert result["results"][0]["listing_evidence"][0]["field"] == "/title"
    filters["exclude_product_ids"] = ["amazon:B000000002"]
    model = FakeEmbedder(fail_after=0)
    assert search_catalog(pg, ProductSearchRequest(query="moisturizer", filters=filters), model)["results"] == []
    assert model.calls == 0
    result = search_catalog(pg, ProductSearchRequest(query="cotton shirt"), FakeEmbedder())
    assert result["results"][0]["product_id"] == "amazon:B000000004"
    for brand, expected in [("o'reilly", 1), ("' OR true --", 0)]:
        result = search_catalog(pg, ProductSearchRequest(query="moisturizer", filters={"brand": brand}), FakeEmbedder())
        assert result["eligible_products"] == expected


def test_catalog_switch_requires_a_matching_complete_search_build(pg, scratch_database, tmp_path):
    make_source(tmp_path)
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        old = build_index(db, FakeEmbedder(), lambda *args, **kwargs: None)
    changed = product("B000000002")
    changed["title"] = "A completely different listing"
    make_source(tmp_path, [changed])
    with validated_source(tmp_path) as source: import_catalog(pg, source)
    pg.commit()
    with pytest.raises(CatalogError, match="No complete search"):
        pin_search_build(pg)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        with pytest.raises(CatalogError):
            build_index(db, FakeEmbedder(fail_after=0), lambda *args, **kwargs: None)
        assert db.execute("SELECT build_id FROM commerce.search_active").fetchone() == (old["build_id"],)


@pytest.mark.parametrize("filters", [
    {"min_price": "30", "max_price": "10"}, {"max_price": "NaN"},
    {"attributes": {"fragrance_free": "true"}}, {"attributes": {"skin_type": True}},
    {"attributes": {"unsupported": "value"}}, {"brand": "\x00"}, {"attributes": {"color": "\x00"}},
])
def test_bad_filters_rejected(filters):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): ProductFilters(**filters)

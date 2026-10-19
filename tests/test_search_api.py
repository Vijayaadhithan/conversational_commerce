from dataclasses import replace

from fastapi.testclient import TestClient

from app.catalog import CatalogError
from app.main import Settings, create_app
import pytest
import httpx
from app.product_search import ProductSearch


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_startup_warmup_is_reported_before_serving(tmp_path, monkeypatch, enabled):
    calls = []
    monkeypatch.setenv("SEARCH_WARMUP", "1" if enabled else "0")
    monkeypatch.setattr(ProductSearch, "warmup", lambda self: calls.append(True) or {"status": "ready", "seconds": .1})
    settings = replace(Settings(), database=str(tmp_path / "warm.sqlite3"))
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": [{"name": settings.model}]}))
    with TestClient(create_app(settings, transport)) as client:
        result = client.get("/health").json()
        assert result["search_warmup"]["status"] == ("ready" if enabled else "disabled")
        assert len(calls) == int(enabled)


def test_search_passes_validated_filters_and_returns_service_results(tmp_path):
    with TestClient(create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))) as client:
        assert client.app.state.product_search.reranker is client.app.state.reranker
        class Search:
            def search(self, request):
                assert request.query == "moisturizer for dry skin"
                assert str(request.filters.max_price) == "30.00"
                assert request.filters.attributes == {"fragrance_free": True}
                assert request.rerank is True
                return {"results": [{"product_id": "amazon:B000000001"}]}
        client.app.state.product_search = Search()
        response = client.post("/products/search", json={"query": "moisturizer for dry skin",
            "filters": {"domain": "beauty", "max_price": "30.00", "attributes": {"fragrance_free": True}}})
        assert response.status_code == 200 and response.json()["results"]
        for body in [{"query": " "}, {"query": "💄" * 251}, {"query": "a", "limit": 0},
                     {"query": "a", "filters": {"stock": True}}, {"query": "a", "extra": True}, {"query": "a", "rerank": "false"}]:
            assert client.post("/products/search", json=body).status_code == 422


def test_unready_search_returns_actionable_service_unavailable(tmp_path):
    with TestClient(create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))) as client:
        class Search:
            def search(self, request):
                raise CatalogError("No complete search build; run scripts/build_search_index.py")
        client.app.state.product_search = Search()
        response = client.post("/products/search", json={"query": "blue shirt"})
        assert response.status_code == 503 and "build_search_index.py" in response.json()["detail"]


def test_search_can_explicitly_disable_reranking(tmp_path):
    with TestClient(create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))) as client:
        class Search:
            def search(self, request): return {"rerank_requested": request.rerank}
        client.app.state.product_search = Search()
        response = client.post("/products/search", json={"query": "shoes", "rerank": False})
        assert response.status_code == 200 and response.json()["rerank_requested"] is False

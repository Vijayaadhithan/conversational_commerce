from dataclasses import replace

from fastapi.testclient import TestClient

from app.catalog import CatalogError
from app.main import Settings, create_app


def test_search_passes_validated_filters_and_returns_service_results(tmp_path):
    with TestClient(create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))) as client:
        class Search:
            def search(self, request):
                assert request.query == "moisturizer for dry skin"
                assert str(request.filters.max_price) == "30.00"
                assert request.filters.attributes == {"fragrance_free": True}
                return {"results": [{"product_id": "amazon:B000000001"}]}
        client.app.state.product_search = Search()
        response = client.post("/products/search", json={"query": "moisturizer for dry skin",
            "filters": {"domain": "beauty", "max_price": "30.00", "attributes": {"fragrance_free": True}}})
        assert response.status_code == 200 and response.json()["results"]
        for body in [{"query": " "}, {"query": "💄" * 251}, {"query": "a", "limit": 0},
                     {"query": "a", "filters": {"stock": True}}, {"query": "a", "extra": True}]:
            assert client.post("/products/search", json=body).status_code == 422


def test_unready_search_returns_actionable_service_unavailable(tmp_path):
    with TestClient(create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))) as client:
        class Search:
            def search(self, request):
                raise CatalogError("No complete search build; run scripts/build_search_index.py")
        client.app.state.product_search = Search()
        response = client.post("/products/search", json={"query": "blue shirt"})
        assert response.status_code == 503 and "build_search_index.py" in response.json()["detail"]

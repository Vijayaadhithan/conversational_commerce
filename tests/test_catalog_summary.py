from dataclasses import replace

from fastapi.testclient import TestClient

from app import routes_search
from app.catalog import CatalogError
from app.main import Settings, create_app


def test_catalog_summary_is_authenticated_and_returns_full_active_coverage(tmp_path, monkeypatch):
    monkeypatch.setenv("API_AUTH_REQUIRED", "1")
    monkeypatch.setattr(
        routes_search,
        "catalog_summary",
        lambda _: {"product_count": 50000, "indexed_product_count": 50000, "categories": []},
    )
    with TestClient(
        create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))
    ) as client:
        assert client.get("/catalog/summary").status_code == 401
        _, token = client.app.state.store.issue_token("demo")
        result = client.get("/catalog/summary", headers={"Authorization": "Bearer " + token})
        assert result.status_code == 200
        assert result.json()["product_count"] == result.json()["indexed_product_count"] == 50000


def test_catalog_summary_failure_hides_database_details(tmp_path, monkeypatch):
    def fail(_):
        raise CatalogError("private connection details")

    monkeypatch.setattr(routes_search, "catalog_summary", fail)
    with TestClient(
        create_app(replace(Settings(), database=str(tmp_path / "chat.sqlite3")))
    ) as client:
        response = client.get("/catalog/summary")
        assert response.status_code == 503
        assert response.json() == {"detail": "Catalog overview unavailable"}

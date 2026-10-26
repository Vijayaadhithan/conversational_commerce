"""Optional serving optimizations must not make unit tests contact local services."""

import os
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app import catalog


@pytest.fixture(autouse=True)
def isolate_optional_serving_features(monkeypatch):
    monkeypatch.setenv("SEARCH_WARMUP", "0")
    monkeypatch.setenv("SEARCH_CACHE_REDIS_URL", "")
    monkeypatch.delenv("SEARCH_BACKEND", raising=False)


@pytest.fixture(autouse=True)
def legacy_local_api(monkeypatch):
    # Existing behavior tests use isolated SQLite files. Security tests override these.
    monkeypatch.setenv("API_AUTH_REQUIRED", "0")
    monkeypatch.setenv("API_REQUIRE_IDEMPOTENCY_KEY", "0")
    monkeypatch.setenv("API_REQUEST_BURST", "10000")
    monkeypatch.setenv("API_REQUESTS_PER_MINUTE", "10000")
    monkeypatch.setenv("API_MODEL_BURST", "10000")
    monkeypatch.setenv("API_MODEL_REQUESTS_PER_MINUTE", "10000")
    monkeypatch.setenv("API_MONITOR_INTERVAL_SECONDS", "0")


@pytest.fixture(scope="session")
def scratch_database():
    admin_url = os.getenv("CATALOG_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set CATALOG_TEST_ADMIN_URL to enable isolated PostgreSQL integration tests")
    name = "commerce_catalog_test_" + uuid4().hex[:16]
    url = make_conninfo(admin_url, dbname=name)
    catalog.create_database(url, admin_url)
    try:
        yield url
    finally:
        with catalog.connect_database(admin_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def pg(scratch_database, monkeypatch):
    from app.qdrant_client import QdrantClient, QdrantSettings

    url = os.getenv("QDRANT_TEST_URL")
    if not url:
        pytest.skip("Set QDRANT_TEST_URL for catalog/search integration tests")
    prefix = "test_" + uuid4().hex[:16]
    monkeypatch.setenv("QDRANT_URL", url)
    monkeypatch.setenv("QDRANT_COLLECTION_PREFIX", prefix)
    monkeypatch.setenv("QDRANT_API_KEY", "")
    monkeypatch.setenv("QDRANT_EXACT", "1")
    qdrant_settings = QdrantSettings.from_env()
    # Only reset the randomly named database created by scratch_database above.
    with catalog.connect_database(scratch_database) as db:
        db.execute("DROP SCHEMA IF EXISTS commerce CASCADE")
        db.commit()
        catalog.apply_migrations(db)
        try:
            yield db
        finally:
            with QdrantClient(qdrant_settings) as client:
                for item in client.request("GET", "/collections")["collections"]:
                    if item["name"].startswith(prefix + "_"):
                        client.request("DELETE", "/collections/" + item["name"], missing_ok=True)

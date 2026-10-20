"""Optional serving optimizations must not make unit tests contact local services."""
import pytest


@pytest.fixture(autouse=True)
def isolate_optional_serving_features(monkeypatch):
    monkeypatch.setenv("SEARCH_WARMUP", "0")
    monkeypatch.setenv("SEARCH_CACHE_REDIS_URL", "")


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

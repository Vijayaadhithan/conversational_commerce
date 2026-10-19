"""Optional serving optimizations must not make unit tests contact local services."""
import pytest


@pytest.fixture(autouse=True)
def isolate_optional_serving_features(monkeypatch):
    monkeypatch.setenv("SEARCH_WARMUP", "0")
    monkeypatch.setenv("SEARCH_CACHE_REDIS_URL", "")

import json
import os
from uuid import uuid4

import pytest
import redis

from app.search_cache import SearchCache
from app.product_search import ProductSearchRequest


class MemoryClient:
    def __init__(self): self.values = {}; self.writes = 0
    def get(self, key): return self.values.get(key)
    def eval(self, script, count, index, key, now, payload, ttl, limit):
        self.writes += 1
        self.values[key] = payload
    def close(self): pass


def test_cache_key_covers_generation_query_filters_concerns_limit_reranker_and_code():
    cache = SearchCache(client=MemoryClient())
    pinned = {"build_id": "first", "config": {"model_digest": "model"}}
    request = ProductSearchRequest(query="moisturizer")
    key = cache.key(request, pinned, {"candidates": 20})
    for changes in ({"query": "shampoo"}, {"filters": {"max_price": "10"}}, {"concerns": ["dry skin"]},
                    {"rerank": False}, {"limit": 1}, {"filters": {"exclude_product_ids": ["amazon:B000000001"]}}):
        altered = ProductSearchRequest.model_validate({**request.model_dump(), **changes})
        assert key != cache.key(altered, pinned, {"candidates": 20})
    assert key != cache.key(request, {**pinned, "build_id": "second"}, {"candidates": 20})
    assert key != cache.key(request, pinned, {"candidates": 30})
    cache.signature = "changed"
    assert key != cache.key(request, pinned, {"candidates": 20})


def test_cached_values_are_detached_and_corruption_or_large_values_are_misses():
    client = MemoryClient(); cache = SearchCache(client=client, max_bytes=1024)
    value = {"results": [{"title": "original"}]}
    cache.put("key", value)
    value["results"][0]["title"] = "changed"
    restored, status = cache.get("key")
    assert status == "hit" and restored["results"][0]["title"] == "original"
    restored["results"].clear()
    assert cache.get("key")[0]["results"]
    client.values["key"] = "not json"
    assert cache.get("key")[0] is None
    cache.put("large", {"results": ["x" * 2000]})
    assert client.writes == 1
    assert SearchCache().get("key") == (None, "disabled")


def test_redis_failure_does_not_break_search_cache_callers():
    class Broken(MemoryClient):
        def get(self, key): raise redis.ConnectionError("offline")
        def eval(self, *args): raise redis.ConnectionError("offline")
    cache = SearchCache(client=Broken())
    assert cache.get("key") == (None, "unavailable")
    cache.put("key", {"results": []})


def test_live_redis_bounds_and_ttl_without_touching_other_keys():
    url = os.getenv("CATALOG_TEST_REDIS_URL")
    if not url: pytest.skip("Set CATALOG_TEST_REDIS_URL for isolated-prefix Redis integration")
    cache = SearchCache(url, ttl=5, max_entries=2)
    cache.prefix = "commerce:test:" + uuid4().hex + ":"
    try:
        for i in range(4): cache.put(cache.prefix + str(i), {"results": [{"product_id": str(i)}]})
        assert cache.client.zcard(cache.prefix + "entries") == 2
        assert cache.get(cache.prefix + "0")[0] is None
        assert cache.get(cache.prefix + "3")[1] == "hit"
        assert 0 < cache.client.ttl(cache.prefix + "3") <= 5
    finally:
        keys = list(cache.client.scan_iter(match=cache.prefix + "*"))
        if keys: cache.client.delete(*keys)
        cache.close()

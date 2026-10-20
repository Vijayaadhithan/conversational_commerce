"""Opt-in Redis result cache, bounded per namespace with short TTL and fail-open IO."""
import hashlib
import json
import os
from pathlib import Path
import time

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

STORE = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _, key in ipairs(expired) do redis.call('DEL', key) end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('ZADD', KEYS[1], ARGV[1] + ARGV[3], KEYS[2])
local excess = redis.call('ZCARD', KEYS[1]) - tonumber(ARGV[4])
if excess > 0 then
  local keys = redis.call('ZRANGE', KEYS[1], 0, excess - 1)
  for _, key in ipairs(keys) do redis.call('DEL', key); redis.call('ZREM', KEYS[1], key) end
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
return 1
"""


def code_signature():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in ("product_search.py", "product_reranking.py", "listing_explanations.py", "listing_quality.py", "search_cache.py", "reranker.py"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


class SearchCache:
    def __init__(self, url=None, *, client=None, ttl=300, max_entries=128, max_bytes=262144):
        if not 1 <= ttl <= 3600 or not 1 <= max_entries <= 1000 or not 1024 <= max_bytes <= 1048576:
            raise ValueError("Invalid search cache bounds")
        self.client = client if client is not None else (redis.Redis.from_url(url, socket_timeout=.2,
            socket_connect_timeout=.2, decode_responses=True, retry=Retry(NoBackoff(), 0)) if url else None)
        self.ttl, self.max_entries, self.max_bytes = ttl, max_entries, max_bytes
        self.signature = code_signature()
        self.prefix = "commerce:search:v1:"

    @classmethod
    def from_env(cls):
        return cls(os.getenv("SEARCH_CACHE_REDIS_URL") or None,
            ttl=int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "300")),
            max_entries=int(os.getenv("SEARCH_CACHE_MAX_ENTRIES", "128")))

    def key(self, request, pinned, runtime):
        body = {"request": request.model_dump(mode="json"), "build_id": pinned["build_id"],
                "model_digest": pinned["config"]["model_digest"], "runtime": runtime, "code": self.signature}
        return self.prefix + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def get(self, key):
        if self.client is None: return None, "disabled"
        try:
            payload = self.client.get(key)
            if payload is None: return None, "miss"
            if not isinstance(payload, str): return None, "invalid"
            if len(payload.encode()) > self.max_bytes: return None, "invalid"
            result = json.loads(payload)
            if not isinstance(result, dict) or not isinstance(result.get("results"), list): return None, "invalid"
            if any(not isinstance(row, dict) for row in result["results"]): return None, "invalid"
            return result, "hit"
        except (redis.RedisError, OSError, ValueError, TypeError):
            return None, "unavailable"

    def put(self, key, value):
        if self.client is None: return
        payload = json.dumps(value, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > self.max_bytes: return
        try:
            self.client.eval(STORE, 2, self.prefix + "entries", key, time.time(), payload, self.ttl, self.max_entries)
        except (redis.RedisError, OSError):
            pass

    def close(self):
        if self.client is not None: self.client.close()

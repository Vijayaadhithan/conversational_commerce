"""Exercise real CPU ranking through a simulated hosted failure (no paid calls)."""

import json
import os
import sys
import time
from pathlib import Path

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.reranker import VoyageReranker
from app.reranker_fallback import CpuFallbackReranker, RerankerWithFallback


def main():
    # This isolated process never sends a credential or request to Voyage.
    os.environ["VOYAGE_API_KEY"] = "offline-canary"
    os.environ.pop("VOYAGE_API_KEY_FILE", None)
    fallback = CpuFallbackReranker()
    ranker = RerankerWithFallback(
        VoyageReranker(transport=httpx.MockTransport(lambda request: httpx.Response(503))),
        fallback,
    )
    cases = [
        (
            "fragrance free moisturizer for dry skin",
            [
                "Leather shoes",
                "Fragrance free moisturizer for dry sensitive skin",
                "Perfumed shampoo",
            ],
            1,
        ),
        (
            "red cotton dress",
            ["Red cotton summer dress", "Blue denim trousers", "Fragrance free cream"],
            0,
        ),
        ("running shoes", ["Gold necklace", "Cotton T shirt", "Lightweight running trainers"], 2),
    ]
    start = time.monotonic()
    for query, documents, expected in cases:
        result = ranker.rank(query, documents, 3, preserve_query=True)
        assert result["results"][0]["index"] == expected
        assert result["device"] == "cpu"
    # Query must never disappear because a document fills the context.
    result = ranker.rank(
        "running shoes", ["unrelated " * 2000, "Running shoes for athletes"], 2, preserve_query=True
    )
    assert result["results"][0]["index"] == 1
    print(
        json.dumps(
            {
                "status": "passed",
                "fallback_calls": ranker.fallback_calls,
                "model": result["model"],
                "seconds": round(time.monotonic() - start, 2),
                "rss_mb": round(psutil.Process().memory_info().rss / 1024**2, 1),
            }
        )
    )


if __name__ == "__main__":
    main()

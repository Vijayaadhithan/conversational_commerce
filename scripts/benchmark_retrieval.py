"""Repeatable local latency observations, with explicit cache and cold-process labels."""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from check_search import CASES
from dotenv import load_dotenv

from app.catalog import ROOT
from app.product_search import ProductSearch, ProductSearchRequest
from app.search_cache import SearchCache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--cache-url", help="Use a specific Redis endpoint for this benchmark")
    parser.add_argument("--disable-cache", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    service = ProductSearch(
        cache=SearchCache()
        if args.disable_cache
        else SearchCache(args.cache_url)
        if args.cache_url
        else None
    )
    warmup = service.warmup() if args.warmup else {"status": "not_requested"}
    observations = []
    for repeat in range(args.repeat):
        for query, filters in CASES:
            start = time.perf_counter()
            result = service.search(
                ProductSearchRequest(query=query, filters=filters, rerank=args.rerank)
            )
            observations.append(
                {
                    "query": query,
                    "repeat": repeat,
                    "seconds": time.perf_counter() - start,
                    "build_id": result["build_id"],
                    "cache": result.get("cache", {"status": "not_implemented"}),
                    "timings": result.get("timings", {}),
                    "eligible_products": result["eligible_products"],
                    "reranking": result.get("reranking", {}),
                    "products": [
                        {"product_id": row["product_id"], "title": row["title"]}
                        for row in result["results"]
                    ],
                }
            )
        print(f"Completed repetition {repeat + 1}/{args.repeat}", flush=True)
    latencies = sorted(row["seconds"] for row in observations)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rerank": args.rerank,
        "cold_definition": "First request in a fresh Python process; OS/database/Ollama caches are not flushed",
        "requests": len(observations),
        "warmup": warmup,
        "p50_seconds": statistics.median(latencies),
        "p95_seconds": latencies[max(0, __import__("math").ceil(len(latencies) * 0.95) - 1)],
        "observations": observations,
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "observations"}, indent=2))


if __name__ == "__main__":
    main()

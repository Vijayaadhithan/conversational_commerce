"""Compare live RRF/reranked candidates and verify constraints; not a qrel benchmark."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from app.catalog import ROOT
from app.product_search import ProductSearch, ProductSearchRequest
from check_search import CASES


def main():
    load_dotenv(ROOT / ".env")
    service = ProductSearch()
    observations = []
    # Check the two known mismatch queries first, then the remaining smoke cases.
    cases = sorted(CASES, key=lambda item: item[0] not in {"running shoes", "razor blades"})
    for query, filters in cases:
        before = service.search(ProductSearchRequest(query=query, filters=filters, limit=20, rerank=False))
        start = time.perf_counter()
        after = service.search(ProductSearchRequest(query=query, filters=filters, limit=20))
        seconds = time.perf_counter() - start
        before_ids = [row["product_id"] for row in before["results"]]
        after_ids = [row["product_id"] for row in after["results"]]
        assert set(after_ids) == set(before_ids) and len(after_ids) == len(set(after_ids))
        assert before["build_id"] == after["build_id"]
        assert before["filters_applied"] == after["filters_applied"]
        for row in after["results"]:
            for key in ("domain", "family"):
                if key in filters: assert row["category"][key] == filters[key]
            if "max_price" in filters: assert Decimal(row["price"]["amount"]) <= Decimal(filters["max_price"])
            for key, value in filters.get("attributes", {}).items():
                assert row["attributes"][key] is not None and row["attributes"][key]["value"] == value
        if after_ids:
            assert after["reranking"]["applied"]
            scores = [row["ranking"]["rerank_score"] for row in after["results"]]
            assert scores == sorted(scores, reverse=True)
        else:
            assert after["reranking"]["reason"] == "no_candidates"
        summarize = lambda rows: [{"product_id": row["product_id"], "title": row["title"], "ranking": row["ranking"]} for row in rows]
        observations.append({"query": query, "filters": filters, "seconds": round(seconds, 4),
            "reranking": after["reranking"], "baseline": summarize(before["results"]), "reranked": summarize(after["results"])})
        top = after["results"][0]["title"] if after_ids else "<empty>"
        print(f"Passed: {query} ({seconds:.3f}s) -> {top[:160]}", flush=True)
    output = ROOT / "data/search/reranking_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"passed": True, "cases": len(observations), "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_type": "live candidate-identity/constraint checks and ranking observations; no human relevance qrels",
        "observations": observations}
    output.write_text(json.dumps(report, indent=2))
    print(f"Passed {len(observations)} cases. Report: {output}")


if __name__ == "__main__":
    main()

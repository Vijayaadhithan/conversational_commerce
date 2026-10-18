"""Live retrieval smoke checks; these are constraint checks, not relevance qrels."""
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

CASES = [
    ("moisturizer for dry skin", {"domain": "beauty", "family": "skin_care", "max_price": "30.00"}),
    ("shampoo for dry hair", {"domain": "beauty", "family": "hair_care", "max_price": "25.00"}),
    ("moisturizer for sensitive skin", {"domain": "beauty", "family": "skin_care", "max_price": "40.00", "attributes": {"fragrance_free": True}}),
    ("red lipstick", {"domain": "beauty", "family": "makeup", "max_price": "25.00"}),
    ("floral perfume", {"domain": "beauty", "family": "fragrance", "max_price": "50.00"}),
    ("razor blades", {"domain": "beauty", "family": "shaving", "max_price": "30.00"}),
    ("crossbody handbag", {"domain": "fashion", "family": "bags", "max_price": "40.00"}),
    ("running shoes", {"domain": "fashion", "family": "footwear", "max_price": "75.00"}),
    ("silver necklace", {"domain": "fashion", "family": "jewelry", "max_price": "50.00"}),
    ("cotton t shirt", {"domain": "fashion", "family": "apparel", "max_price": "30.00"}),
    ("shirt", {"domain": "fashion", "max_price": "0.00"}),
    ("moisturizer", {"brand": "__catalog_smoke_nonexistent_brand__"}),
]


def main():
    load_dotenv(ROOT / ".env")
    service = ProductSearch()
    observed = []
    for query, filters in CASES:
        start = time.monotonic()
        response = service.search(ProductSearchRequest(query=query, filters=filters, limit=5, rerank=False))
        seconds = time.monotonic() - start
        rows = response["results"]
        assert len(rows) == len({row["product_id"] for row in rows})
        assert len(rows) <= 5
        for row in rows:
            assert row["availability"] is None
            assert row["price"]["currency"] == "USD" and row["price"]["status"] == "historical"
            for key in ("domain", "family"):
                if key in filters: assert row["category"][key] == filters[key]
            if "max_price" in filters: assert Decimal(row["price"]["amount"]) <= Decimal(filters["max_price"])
            for key, value in filters.get("attributes", {}).items():
                assert row["attributes"][key] is not None and row["attributes"][key]["value"] == value
        if response["eligible_products"] == 0: assert rows == []
        else: assert len(rows) == min(5, response["eligible_products"])
        observed.append({"query": query, "filters": filters, "seconds": round(seconds, 4),
            "eligible_products": response["eligible_products"], "build_id": response["build_id"],
            "results": [{"product_id": row["product_id"], "title": row["title"],
                         "historical_price": row["price"]["amount"], "ranking": row["ranking"]} for row in rows]})
        print(f"Passed: {query} ({len(rows)} results, {seconds:.3f}s)", flush=True)
    output = ROOT / "data/search/smoke_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"passed": True, "cases": len(observed), "created_at": datetime.now(timezone.utc).isoformat(),
              "evaluation_type": "live execution and structured-constraint smoke checks; no human relevance judgments",
              "observations": observed}
    output.write_text(json.dumps(report, indent=2))
    print(f"Passed {len(observed)} cases. Report: {output}")


if __name__ == "__main__":
    main()

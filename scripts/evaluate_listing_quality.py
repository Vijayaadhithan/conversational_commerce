"""Replay saved search requests and verify listing evidence against the pinned catalog.

This measures known regression cases, not held-out relevance or human satisfaction.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

from app.catalog import ROOT, connect_database
from app.commerce import explain_products
from app.commerce_models import Concern, ShoppingTask
from app.listing_explanations import evidence_fields
from app.product_search import ProductSearch, ProductSearchRequest, filter_sql, product_goal_sql
from app.search_cache import SearchCache, code_signature


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/listing_quality.json")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/evaluation/listing_quality.json"
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    service = ProductSearch(cache=SearchCache())
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "human_reviewed": False,
        "evaluation_type": "Known search regression cases; source provenance and structured-filter assertions",
        "code_sha256": code_signature(),
        "case_set_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in json.loads(args.cases.read_text()):
        request = ProductSearchRequest.model_validate(case["request"])
        result = service.search(request)
        task = ShoppingTask(
            id=1,
            query=request.product_goal,
            filters=request.filters,
            needs=[Concern(value=c, evidence=c) for c in request.concerns],
        )
        result["results"] = explain_products(result["results"], task)
        with connect_database(
            os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
        ) as db:
            suffix, params = filter_sql(request.filters)
            goal_sql, goal_params = product_goal_sql(request.product_goal)
            for row in result["results"]:
                stored = db.execute(
                    """SELECT p.document FROM commerce.catalog_products p
                    JOIN commerce.search_builds b ON b.import_id=p.import_id
                    WHERE b.build_id=%s AND p.product_id=%s"""
                    + suffix
                    + goal_sql,
                    [result["build_id"], row["product_id"], *params, *goal_params],
                ).fetchone()
                assert stored is not None, "Returned product violated a filter or identity gate"
                assert row["attributes"] == stored[0]["attributes"]
                fields = list(evidence_fields(stored[0]))
                excerpts = [e for item in row["concern_evidence"] for e in item["evidence"]]
                excerpts += [
                    e
                    for c in row["listing_quality"]["cautions"]
                    for e in [c["evidence"], *c.get("other_evidence", [])]
                ]
                assert all(
                    any(f == e["field"] and e["text"] in text for f, text in fields)
                    for e in excerpts
                ), "Quote missing from its source"
        report["cases"].append({**case, "response": result, "checks_passed": True})
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            f"{case['id']}: {len(result['results'])} products; filters and source excerpts passed",
            flush=True,
        )
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()

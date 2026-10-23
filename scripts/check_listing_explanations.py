"""Check live retrieval and quote provenance; this is not a relevance evaluation."""

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
from app.product_search import ProductSearch, ProductSearchRequest


def main():
    load_dotenv(ROOT / ".env")
    search = ProductSearch()
    observations = []
    cases = [
        (
            "moisturizer for dry skin and brightening",
            ["dry skin", "lightens my face", "kess chemincal"],
            {"domain": "beauty", "family": "skin_care", "max_price": "30.00"},
        ),
        (
            "cotton shirt with pockets",
            ["cotton", "pockets"],
            {"domain": "fashion", "family": "apparel", "max_price": "40.00"},
        ),
    ]
    for query, concerns, filters in cases:
        request = ProductSearchRequest(query=query, concerns=concerns, filters=filters, limit=3)
        response = search.search(request)
        assert response["results"] and response["reranking"]["applied"]
        task = ShoppingTask(
            id=1,
            query=query,
            filters=request.filters,
            needs=[Concern(value=value, evidence=value) for value in concerns],
        )
        products = explain_products(response["results"], task)
        with connect_database(
            os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
        ) as db:
            for row in products:
                stored = db.execute(
                    """SELECT p.document FROM commerce.catalog_products p
                    JOIN commerce.search_builds b ON p.import_id=b.import_id
                    WHERE b.build_id=%s AND p.product_id=%s""",
                    (response["build_id"], row["product_id"]),
                ).fetchone()[0]
                assert row["attributes"] == stored["attributes"]
                assert row["price"] == stored["price"]
                assert row["selected_observation"] == stored["selected_observation"]
                assert [item["concern"] for item in row["concern_evidence"]] == concerns
                source_fields = list(evidence_fields(stored))
                for item in row["concern_evidence"]:
                    for evidence in item["evidence"]:
                        assert any(
                            field == evidence["field"] and evidence["text"] in text
                            for field, text in source_fields
                        )
                    if item["concern"] == "kess chemincal":
                        assert item["status"] == "needs_clarification" and not item["evidence"]
                assert row["variant_metadata"]["purchasable_variants"] is None
        observations.append(
            {"query": query, "build_id": response["build_id"], "products": products}
        )
        print(f"Verified {len(products)} product cards for {query}", flush=True)
    output = ROOT / "data/search/listing_explanations_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "passed": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "evaluation_type": "Live retrieval, reranking and exact quote provenance; no human relevance judgments",
                "observations": observations,
            },
            indent=2,
        )
    )
    print(f"Report: {output}")


if __name__ == "__main__":
    main()

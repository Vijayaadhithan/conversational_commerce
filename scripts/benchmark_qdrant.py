"""Compare filtered Qdrant ANN recall with exact PostgreSQL using stored vectors.

No embedding/LLM calls. These are document-vector probes, not a shopping relevance evaluation.
"""

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg
from dotenv import load_dotenv

from app.catalog import ROOT, CatalogError, connect_database
from app.product_search import pin_search_build
from app.qdrant_client import QdrantClient, QdrantSettings, point_id
from app.qdrant_index import pin_qdrant_index
from app.storefronts import FAMILY_STOREFRONT, STOREFRONTS


def benchmark(db, client, samples, k, minimum):
    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    pinned = pin_search_build(db)
    collection = pin_qdrant_index(db, pinned, client)
    probes = db.execute(
        """SELECT d.product_id,d.embedding::text,p.family FROM commerce.search_documents d
        JOIN commerce.catalog_products p ON (p.import_id,p.product_id)=(d.import_id,d.product_id)
        WHERE d.build_id=%s ORDER BY md5(d.product_id) LIMIT %s""",
        (pinned["build_id"], samples),
    ).fetchall()
    if not probes:
        raise CatalogError("Cannot benchmark an empty catalog")
    cases = []
    for key, vector, family in probes:
        scope = FAMILY_STOREFRONT[family]
        families = list(STOREFRONTS[scope]["families"])
        started = time.perf_counter()
        eligible = [
            row[0]
            for row in db.execute(
                "SELECT product_id FROM commerce.catalog_products WHERE import_id=%s AND family=ANY(%s) ORDER BY product_id LIMIT %s",
                (pinned["import_id"], families, client.settings.max_eligible + 1),
            ).fetchall()
        ]
        if len(eligible) > client.settings.max_eligible:
            raise CatalogError("Benchmark scope exceeds QDRANT_MAX_ELIGIBLE")
        eligibility_seconds = time.perf_counter() - started
        started = time.perf_counter()
        baseline = db.execute(
            """SELECT product_id,(embedding <=> %s::public.vector) AS distance
            FROM commerce.search_documents WHERE build_id=%s AND product_id=ANY(%s)
            ORDER BY distance,product_id LIMIT %s""",
            (vector, pinned["build_id"], eligible, k),
        ).fetchall()
        pg_seconds = time.perf_counter() - started
        started = time.perf_counter()
        hits = client.request(
            "POST",
            f"/collections/{collection}/points/query",
            {
                "query": json.loads(vector),
                "using": "dense",
                "limit": k,
                "params": {"exact": False, "hnsw_ef": 128},
                "with_payload": True,
                "filter": {
                    "must": [
                        {"has_id": [point_id(item) for item in eligible]},
                        {"key": "storefront_id", "match": {"value": scope}},
                        {"key": "build_id", "match": {"value": pinned["build_id"]}},
                    ]
                },
            },
        )["points"]
        qdrant_seconds = time.perf_counter() - started
        allowed = set(eligible)
        returned = [row["payload"]["product_id"] for row in hits]
        if len(returned) != len(set(returned)) or any(item not in allowed for item in returned):
            raise CatalogError("Benchmark returned duplicate or out-of-scope candidates")
        # A different ID at the exact cutoff can be an equally good tied neighbor.
        distances = dict(
            db.execute(
                "SELECT product_id,(embedding <=> %s::public.vector) FROM commerce.search_documents WHERE build_id=%s AND product_id=ANY(%s)",
                (vector, pinned["build_id"], returned),
            ).fetchall()
        )
        cutoff = baseline[-1][1]
        recall = sum(distances.get(item, float("inf")) <= cutoff + 1e-6 for item in returned) / len(
            baseline
        )
        cases.append(
            {
                "probe_product_id": key,
                "storefront_id": scope,
                "eligible": len(eligible),
                "tie_adjusted_recall_at_k": recall,
                "eligibility_seconds": eligibility_seconds,
                "postgres_dense_seconds": pg_seconds,
                "qdrant_dense_seconds": qdrant_seconds,
            }
        )

    def timing(field):
        values = sorted(row[field] for row in cases)
        return {
            "p50": statistics.median(values),
            "p95": values[max(0, (95 * len(values) + 99) // 100 - 1)],
        }

    minimum_observed = min(row["tie_adjusted_recall_at_k"] for row in cases)
    return {
        "build_id": pinned["build_id"],
        "collection": collection,
        "k": k,
        "probes": len(cases),
        "probe_type": "stored_document_vectors; not user-query relevance",
        "min_recall": minimum_observed,
        "required_min_recall": minimum,
        "recall_gate_passed": minimum_observed >= minimum,
        "timings": {
            field: timing(field)
            for field in ("eligibility_seconds", "postgres_dense_seconds", "qdrant_dense_seconds")
        },
        "cases": cases,
    }


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=24)
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument("--min-recall", type=float, default=0.98)
    args = parser.parse_args()
    if not 1 <= args.samples <= 500 or not 1 <= args.k <= 50 or not 0 < args.min_recall <= 1:
        parser.error("samples must be 1..500, k 1..50, min-recall >0..1")
    try:
        with connect_database(
            os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
        ) as db:
            with QdrantClient(replace(QdrantSettings.from_env(), exact=False)) as client:
                result = benchmark(db, client, args.samples, args.k, args.min_recall)
        print(json.dumps(result, indent=2))
        return 0 if result["recall_gate_passed"] else 1
    except (CatalogError, psycopg.Error, OSError, ValueError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, CatalogError)
            else "Check database and service configuration"
        )
        print(f"Qdrant benchmark failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

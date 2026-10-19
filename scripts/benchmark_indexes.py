"""Measure exact vs HNSW on a disposable copy of one pinned catalog generation."""
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from app.catalog import ROOT, connect_database
from app.embeddings import EmbeddingSettings, OllamaEmbedder, vector_literal
from app.embedding_text import QUERY_PREFIX
from app.product_search import ProductFilters, filter_sql, pin_search_build
from check_search import CASES


def main():
    load_dotenv(ROOT / ".env")
    report = {"purpose": "Vector candidate recall against exact search, not human recommendation relevance", "observations": []}
    with connect_database(os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")) as db:
        pinned = pin_search_build(db)
        report.update(build_id=pinned["build_id"], products=pinned["catalog_products"],
                      pgvector=db.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0])
        db.execute("SET LOCAL maintenance_work_mem='64MB'")
        db.execute("SET LOCAL work_mem='16MB'")
        db.execute("""CREATE TEMP TABLE benchmark_vectors ON COMMIT DROP AS
            SELECT p.product_id,p.domain,p.family,p.brand,p.historical_price,p.attributes,d.embedding
            FROM commerce.search_documents d JOIN commerce.catalog_products p
              ON (p.import_id,p.product_id)=(d.import_id,d.product_id) WHERE d.build_id=%s""", (pinned["build_id"],))
        started = time.perf_counter()
        db.execute("CREATE INDEX benchmark_hnsw ON benchmark_vectors USING hnsw (embedding public.vector_cosine_ops)")
        db.execute("ANALYZE benchmark_vectors")
        report["index_build_seconds"] = time.perf_counter() - started
        report["index_bytes"] = db.execute("SELECT pg_relation_size('benchmark_hnsw')").fetchone()[0]
        with OllamaEmbedder(EmbeddingSettings.from_env()) as embedder:
            embedder.verify_identity(pinned["config"]["model_digest"])
            for query, filters in CASES + [("moisturizer", {}), ("shirt", {"domain": "fashion"})]:
                vector = vector_literal(embedder.embed([QUERY_PREFIX + query])[0])
                suffix, params = filter_sql(ProductFilters.model_validate(filters))
                statement = "SELECT p.product_id FROM benchmark_vectors p WHERE true" + suffix + " ORDER BY p.embedding <=> %s::public.vector LIMIT 50"
                runs = {}
                for mode in ("exact", "hnsw_40", "hnsw_100", "hnsw_200"):
                    db.execute("SET LOCAL enable_indexscan=" + ("off" if mode == "exact" else "on"))
                    db.execute("SET LOCAL enable_seqscan=" + ("on" if mode == "exact" else "off"))
                    if mode != "exact":
                        db.execute("SELECT set_config('hnsw.ef_search',%s,true)", (mode.split("_")[1],))
                        db.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
                    seconds = []
                    for _ in range(5):
                        start = time.perf_counter()
                        ids = [row[0] for row in db.execute(statement, [*params, vector])]
                        seconds.append(time.perf_counter() - start)
                    plan = db.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + statement, [*params, vector]).fetchone()[0]
                    runs[mode] = {"ids": ids, "seconds": seconds, "median_seconds": statistics.median(seconds), "plan": plan}
                    if mode != "exact":
                        exact = set(runs["exact"]["ids"])
                        runs[mode]["recall_at_50"] = len(exact.intersection(ids)) / len(exact) if exact else None
                report["observations"].append({"query": query, "filters": filters, "runs": runs})
                print(f"Benchmarked exact/HNSW: {query}", flush=True)
    summaries = {}
    for mode in ("exact", "hnsw_40", "hnsw_100", "hnsw_200"):
        times = [item["runs"][mode]["median_seconds"] for item in report["observations"]]
        recalls = [item["runs"][mode].get("recall_at_50") for item in report["observations"]]
        recalls = [r for r in recalls if r is not None]
        summaries[mode] = {"median_query_seconds": statistics.median(times), "worst_query_seconds": max(times),
            "mean_candidate_recall": statistics.mean(recalls) if recalls else None,
            "minimum_candidate_recall": min(recalls) if recalls else None}
    report["summary"] = summaries
    output = ROOT / "data/evaluation/index_benchmark_50k.json"
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(report, indent=2))
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__": main()

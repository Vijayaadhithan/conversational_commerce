"""Grow an existing sample from a verified normalized archive using bounded RAM."""
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.catalog import CatalogError, _read_rows, strict_json, validated_source
from app.enrichment_pipeline import fingerprint, write_json


def expand_sample(source_dir, output_dir, size, seed="commerce-scale-v1", progress=print):
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    if source_dir == output_dir: raise CatalogError("Use a separate sampling output directory")
    with validated_source(source_dir) as source:
        original, original_id, old_count = source.manifest, source.import_id, source.count
    if size < old_count: raise CatalogError("Expansion must retain the existing sample")
    pointer = strict_json((source_dir / "current.json").read_bytes())
    source_run = (source_dir / pointer["manifest"]).resolve().parent
    archive = source_run / "catalog.jsonl.gz"
    if fingerprint(archive) != original["outputs"]["catalog.jsonl.gz"]:
        raise CatalogError("Normalized catalog archive differs from original manifest")
    signature = {"source_import_id": original_id, "target": size, "seed": seed,
                 "code_sha256": fingerprint(Path(__file__))["sha256"]}
    run_id = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    run = output_dir / "runs" / run_id
    if not run.exists():
        building = output_dir / "runs" / (run_id + ".building-" + uuid4().hex)
        building.mkdir(parents=True)
        with TemporaryDirectory(prefix="sampling-", dir=building) as tmp:
            db = sqlite3.connect(Path(tmp) / "candidates.sqlite3")
            try:
                db.execute("PRAGMA cache_size=-16384")
                db.execute("PRAGMA temp_store=FILE")
                db.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY, stratum TEXT, rank TEXT, payload TEXT, selected INTEGER)")
                def insert(record, selected):
                    stratum = json.dumps([record["selected_observation"]["source"], record["category"]["domain"], record["category"]["family"]])
                    rank = hashlib.sha256((seed + "\0" + record["product_id"]).encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?)", (record["product_id"], stratum, rank,
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")), selected))
                with validated_source(source_dir) as source:
                    if source.import_id != original_id: raise CatalogError("Sampling source changed")
                    for _, _, record in source.rows(): insert(record, 1)
                db.commit()
                with gzip.open(archive, "rb") as stream:
                    count = 0
                    for count, _, record in _read_rows(stream):
                        if record["quality"]["eligible_for_subset"]: insert(record, 0)
                        if count % 10000 == 0: db.commit()
                        if count % 250000 == 0: progress(f"Scanned {count:,} normalized records", flush=True)
                if count != original["counts"]["unique_products"]: raise CatalogError("Normalized archive row count differs")
                db.commit()
                db.execute("CREATE INDEX candidates_sample ON candidates(selected,stratum,rank,id)")
                strata = dict(db.execute("SELECT stratum,count(*) FROM candidates WHERE selected=0 GROUP BY stratum"))
                if size > old_count + sum(strata.values()): raise CatalogError("Requested sample exceeds eligible catalog")
                quotas = Counter()
                remaining = size - old_count
                while remaining:
                    available = [key for key in sorted(strata) if quotas[key] < strata[key]]
                    step = max(1, remaining // len(available))
                    for key in available:
                        take = min(step, remaining, strata[key] - quotas[key])
                        quotas[key] += take; remaining -= take
                for key, quota in quotas.items():
                    db.execute("UPDATE candidates SET selected=1 WHERE id IN (SELECT id FROM candidates WHERE selected=0 AND stratum=? ORDER BY rank,id LIMIT ?)", (key, quota))
                db.commit()
                with (building / "subset.jsonl").open("w") as stream:
                    for (payload,) in db.execute("SELECT payload FROM candidates WHERE selected=1 ORDER BY id"):
                        stream.write(payload + "\n")
                coverage = dict(db.execute("SELECT stratum,count(*) FROM candidates WHERE selected=1 GROUP BY stratum"))
            finally:
                db.close()
        manifest = {"schema_version": original["schema_version"], "schema_sha256": original["schema_sha256"],
                    "sources": original["sources"], "run_id": run_id, "subset_size": size,
                    "counts": {"subset_rows": size}, "sampling": signature,
                    "outputs": {"subset.jsonl": fingerprint(building / "subset.jsonl")}}
        write_json(building / "manifest.json", manifest)
        write_json(building / "sampling_report.json", {"retained": old_count, "added": size - old_count, "products": size,
            "strata": coverage, "working_storage": "SQLite on disk, 16 MiB page cache"})
        with validated_source(output_dir, building / "manifest.json"): pass
        building.rename(run)
    with validated_source(output_dir, run / "manifest.json") as source:
        if source.manifest.get("sampling") != signature: raise CatalogError("Existing sample signature differs")
    temporary = output_dir / (uuid4().hex + ".tmp")
    write_json(temporary, {"run_id": run_id, "manifest": f"runs/{run_id}/manifest.json"})
    os.replace(temporary, output_dir / "current.json")
    return {"manifest": str(run / "manifest.json"), "products": size, "retained": old_count}

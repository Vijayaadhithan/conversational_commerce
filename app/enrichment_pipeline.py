"""Publish an enriched subset without altering the ETL run or its membership."""

from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
import os
import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path
from uuid import uuid4

from app import catalog_enrichment, listing_quality
from app.catalog import CatalogError, strict_json, validated_source


def fingerprint(path):
    with path.open("rb") as stream:
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "bytes": path.stat().st_size}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def publish_enrichment(source_dir, raw_dir, output_dir, progress=print):
    source_dir, raw_dir, output_dir = map(lambda p: Path(p).resolve(), (source_dir, raw_dir, output_dir))
    if source_dir == output_dir:
        raise CatalogError("Enrichment output must be separate from the source run")
    with validated_source(source_dir) as source:
        source_manifest = deepcopy(source.manifest)
        source_id = source.import_id
        count = source.count
    if source_manifest.get("derivation"):
        raise CatalogError("Use the original ETL source, not an already enriched snapshot")
    signature = {"version": catalog_enrichment.VERSION, "source_import_id": source_id,
                 "code_sha256": fingerprint(Path(catalog_enrichment.__file__))["sha256"],
                 "identity_sha256": fingerprint(Path(listing_quality.__file__))["sha256"],
                 "pipeline_sha256": fingerprint(Path(__file__))["sha256"]}
    run_id = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    run = output_dir / "runs" / run_id
    if run.exists():
        with validated_source(output_dir, run / "manifest.json") as existing:
            report = strict_json((run / "enrichment_report.json").read_bytes())
            if existing.manifest.get("derivation") != signature or fingerprint(run / "enrichment_report.json") != existing.manifest["outputs"]["enrichment_report.json"]:
                raise CatalogError("Existing enrichment differs from its manifest")
    else:
        building = output_dir / "runs" / (run_id + ".building-" + uuid4().hex)
        building.mkdir(parents=True)
        before, after = Counter(), Counter()
        changed, found, examples = 0, 0, []
        # Payloads, source-row lookup and ordering live on disk, not in Python lists.
        with TemporaryDirectory(prefix="enrichment-work-", dir=building) as tmp:
            db = sqlite3.connect(Path(tmp) / "work.sqlite3")
            try:
                db.execute("PRAGMA cache_size=-16384")
                db.execute("PRAGMA temp_store=FILE")
                db.execute("CREATE TABLE records (ordinal INTEGER PRIMARY KEY, source TEXT, row_number INTEGER, payload TEXT, UNIQUE(source,row_number))")
                with validated_source(source_dir) as source:
                    if source.import_id != source_id: raise CatalogError("Source changed during enrichment")
                    for ordinal, _, record in source.rows():
                        observation = record["selected_observation"]
                        db.execute("INSERT INTO records VALUES (?,?,?,?)", (ordinal, observation["source"], observation["row"], json.dumps(record)))
                        before.update(name for name, value in record["attributes"].items() if value)
                        if ordinal % 1000 == 0: db.commit()
                db.commit()
                for spec in source_manifest["sources"]:
                    archive = (raw_dir / spec["filename"]).resolve()
                    if not archive.is_relative_to(raw_dir): raise CatalogError("Raw archive escapes source directory")
                    if fingerprint(archive)["sha256"] != spec["sha256"]:
                        raise CatalogError("Raw archive checksum differs from the source manifest")
                    progress(f"Enriching selected observations from {spec['name']}", flush=True)
                    cursor = iter(db.execute("SELECT row_number,ordinal,payload FROM records WHERE source=? ORDER BY row_number", (spec["name"],)))
                    selected = next(cursor, None)
                    with gzip.open(archive, "rb") as stream:
                        row_number = 0
                        for row_number, line in enumerate(stream, 1):
                            if selected is None or row_number != selected[0]: continue
                            record = strict_json(selected[2])
                            if hashlib.sha256(line).hexdigest() != record["selected_observation"]["record_sha256"]:
                                raise CatalogError("Selected raw observation checksum differs")
                            raw = strict_json(line)
                            if raw.get("parent_asin") != record["parent_asin"]:
                                raise CatalogError("Selected raw observation product ID differs")
                            enriched = catalog_enrichment.enrich_record(record, raw)
                            additions = {name: value for name, value in enriched["attributes"].items() if value and not record["attributes"].get(name)}
                            changed += bool(additions)
                            if additions and len(examples) < 12:
                                examples.append({"product_id": record["product_id"], "added": additions})
                            after.update(name for name, value in enriched["attributes"].items() if value)
                            db.execute("UPDATE records SET payload=? WHERE ordinal=?", (json.dumps(enriched, ensure_ascii=False, separators=(",", ":")), selected[1]))
                            found += 1
                            selected = next(cursor, None)
                            if found % 1000 == 0: db.commit()
                        if row_number != spec["expected_rows"] or selected is not None:
                            raise CatalogError("Raw archive row count or selected observations differ from manifest")
                    db.commit()
                if found != count: raise CatalogError("Some selected observations were not found")
                with (building / "subset.jsonl").open("w") as stream:
                    for (payload,) in db.execute("SELECT payload FROM records ORDER BY ordinal"):
                        stream.write(payload + "\n")
            finally:
                db.close()
        report = {"version": catalog_enrichment.VERSION, "products": count, "products_enriched": changed,
                  "source_run_id": source_manifest["run_id"], "run_id": run_id,
                  "working_storage": "SQLite on disk; 16 MiB page cache; one source record at a time",
                  "coverage": {name: {"before": before[name], "after": after[name], "added": after[name] - before[name], "unknown": count - after[name]}
                               for name in sorted(before.keys() | after.keys())},
                  "purchasable_variants": "unknown: parent listings do not identify purchasable variants or current stock",
                  "examples": examples}
        write_json(building / "enrichment_report.json", report)
        manifest = {"schema_version": source_manifest["schema_version"], "schema_sha256": source_manifest["schema_sha256"],
                    "run_id": run_id, "subset_size": count, "counts": {"subset_rows": count},
                    "sources": source_manifest["sources"], "derivation": signature,
                    "outputs": {name: fingerprint(building / name) for name in ("subset.jsonl", "enrichment_report.json")}}
        write_json(building / "manifest.json", manifest)
        with validated_source(output_dir, building / "manifest.json"):
            pass
        # Never overwrite an existing immutable run; competing publication fails safely.
        building.rename(run)
    pointer = output_dir / ("current." + uuid4().hex + ".tmp")
    write_json(pointer, {"run_id": run_id, "manifest": f"runs/{run_id}/manifest.json"})
    os.replace(pointer, output_dir / "current.json")
    return {"status": "ready", "manifest": str(run / "manifest.json"), "report": str(run / "enrichment_report.json"),
            "products": report["products"], "products_enriched": report["products_enriched"]}

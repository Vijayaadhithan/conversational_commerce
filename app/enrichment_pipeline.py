"""Publish an enriched subset without altering the ETL run or its membership."""

from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from app import catalog_enrichment
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
        records = [record for _, _, record in source.rows()]
    if source_manifest.get("derivation"):
        raise CatalogError("Use the original ETL source, not an already enriched snapshot")
    signature = {"version": catalog_enrichment.VERSION, "source_import_id": source_id,
                 "code_sha256": fingerprint(Path(catalog_enrichment.__file__))["sha256"],
                 "pipeline_sha256": fingerprint(Path(__file__))["sha256"]}
    run_id = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:20]
    run = output_dir / "runs" / run_id
    if run.exists():
        with validated_source(output_dir, run / "manifest.json") as existing:
            report = strict_json((run / "enrichment_report.json").read_bytes())
            if existing.manifest.get("derivation") != signature or fingerprint(run / "enrichment_report.json") != existing.manifest["outputs"]["enrichment_report.json"]:
                raise CatalogError("Existing enrichment differs from its manifest")
    else:
        before = Counter(name for record in records for name, value in record["attributes"].items() if value)
        selected = {(r["selected_observation"]["source"], r["selected_observation"]["row"]): index for index, r in enumerate(records)}
        if len(selected) != len(records): raise CatalogError("Selected source rows are not unique")
        found = set()
        changed = 0
        examples = []
        for spec in source_manifest["sources"]:
            archive = (raw_dir / spec["filename"]).resolve()
            if not archive.is_relative_to(raw_dir): raise CatalogError("Raw archive escapes source directory")
            if fingerprint(archive)["sha256"] != spec["sha256"]:
                raise CatalogError("Raw archive checksum differs from the source manifest")
            progress(f"Enriching selected observations from {spec['name']}", flush=True)
            with gzip.open(archive, "rb") as stream:
                row_number = 0
                for row_number, line in enumerate(stream, 1):
                    key = (spec["name"], row_number)
                    if key not in selected: continue
                    index = selected[key]
                    record = records[index]
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
                    records[index] = enriched
                    found.add(key)
                if row_number != spec["expected_rows"]:
                    raise CatalogError("Raw archive row count differs from manifest")
        if found != selected.keys(): raise CatalogError("Some selected observations were not found")
        after = Counter(name for record in records for name, value in record["attributes"].items() if value)
        report = {"version": catalog_enrichment.VERSION, "products": len(records), "products_enriched": changed,
                  "source_run_id": source_manifest["run_id"], "run_id": run_id,
                  "coverage": {name: {"before": before[name], "after": after[name], "added": after[name] - before[name], "unknown": len(records) - after[name]}
                               for name in sorted(before.keys() | after.keys())},
                  "purchasable_variants": "unknown: parent listings do not identify purchasable variants or current stock",
                  "examples": examples}
        building = output_dir / "runs" / (run_id + ".building-" + uuid4().hex)
        building.mkdir(parents=True)
        with (building / "subset.jsonl").open("w") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        write_json(building / "enrichment_report.json", report)
        manifest = {"schema_version": source_manifest["schema_version"], "schema_sha256": source_manifest["schema_sha256"],
                    "run_id": run_id, "subset_size": len(records), "counts": {"subset_rows": len(records)},
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

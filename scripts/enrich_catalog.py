"""Enrich the existing subset using checksum-verified selected raw observations."""

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

from app.catalog import ROOT, CatalogError
from app.enrichment_pipeline import publish_enrichment


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--etl-output-dir",
        default=os.getenv(
            "CATALOG_ENRICHMENT_SOURCE_DIR", "../conversational_commerce_etl/data/processed"
        ),
    )
    parser.add_argument("--raw-dir", default="data/public_sources/raw")
    parser.add_argument(
        "--output-dir", default=os.getenv("CATALOG_ENRICHMENT_OUTPUT_DIR", "data/enriched_duckdb")
    )
    args = parser.parse_args()
    try:
        started = time.perf_counter()
        result = publish_enrichment(args.etl_output_dir, args.raw_dir, args.output_dir)
        result.update(
            seconds=time.perf_counter() - started,
            peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
        )
        Path(args.output_dir, "execution_report.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return 0
    except (CatalogError, OSError, ValueError, KeyError, TypeError) as exc:
        message = (
            str(exc) if isinstance(exc, CatalogError) else "Invalid or unreadable enrichment input"
        )
        print(
            f"Enrichment failed: {message}. The active database catalog was not changed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

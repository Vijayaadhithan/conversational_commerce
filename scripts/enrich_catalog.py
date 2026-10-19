"""Enrich the existing subset using checksum-verified selected raw observations."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from app.catalog import ROOT, CatalogError
from app.enrichment_pipeline import publish_enrichment


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etl-output-dir", default=os.getenv("CATALOG_ENRICHMENT_SOURCE_DIR", "../conversational_commerce_etl/data/processed"))
    parser.add_argument("--raw-dir", default="data/public_sources/raw")
    parser.add_argument("--output-dir", default="data/enriched")
    args = parser.parse_args()
    try:
        print(json.dumps(publish_enrichment(args.etl_output_dir, args.raw_dir, args.output_dir), indent=2))
        return 0
    except (CatalogError, OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, CatalogError) else "Invalid or unreadable enrichment input"
        print(f"Enrichment failed: {message}. The active database catalog was not changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

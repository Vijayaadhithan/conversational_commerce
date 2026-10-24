#!/usr/bin/env python3
"""Read-only storefront coverage audit of the currently serving catalog/index snapshot."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from app.catalog_summary import catalog_summary


def main():
    load_dotenv()
    summary = catalog_summary(
        os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
    )
    print(json.dumps(summary, indent=2))
    if (
        summary["unmapped_product_count"]
        or sum(section["product_count"] for section in summary["storefronts"])
        != summary["product_count"]
    ):
        raise SystemExit("Storefront coverage is incomplete; no catalog data was changed.")
    if summary["product_count"] != summary["indexed_product_count"]:
        raise SystemExit("Active catalog/index counts differ; no catalog data was changed.")


if __name__ == "__main__":
    main()

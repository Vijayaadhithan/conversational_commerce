"""Export/verify existing PostgreSQL embeddings in Qdrant without model calls."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg
from dotenv import load_dotenv

from app.catalog import ROOT, CatalogError, connect_database
from app.product_search import pin_search_build
from app.qdrant_client import QdrantClient, QdrantSettings
from app.qdrant_index import sync_qdrant_index, verify_qdrant_index
from app.search_index import verify_build
from app.search_maintenance import maintenance_lock


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", help="Defaults to the active PostgreSQL search generation")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        with (
            connect_database(
                os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce"),
                autocommit=True,
            ) as db,
            QdrantClient(QdrantSettings.from_env()) as client,
            maintenance_lock(db),
        ):
            build_id = args.build_id or pin_search_build(db)["build_id"]
            if args.verify_only:
                with db.transaction():
                    verify_build(db, build_id)
                result = verify_qdrant_index(db, client, build_id)
            else:
                result = sync_qdrant_index(db, client, build_id)
            print(json.dumps(result, indent=2))
        return 0
    except (CatalogError, psycopg.Error, OSError, ValueError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, CatalogError)
            else "Check catalog schema and service configuration"
        )
        print(f"Qdrant export failed: {detail}. Completed batches are retained.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

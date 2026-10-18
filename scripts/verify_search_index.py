"""Independently verify every published search input against its catalog source."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
import psycopg
from app.catalog import ROOT, CatalogError, connect_database
from app.product_search import pin_search_build
from app.search_index import verify_build


def main():
    load_dotenv(ROOT / ".env")
    try:
        with connect_database(os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            build = pin_search_build(db)
            result = verify_build(db, build["build_id"])
        print(json.dumps({"status": "verified", "build_id": build["build_id"], **result}, indent=2))
        return 0
    except (CatalogError, psycopg.Error, OSError) as exc:
        message = str(exc) if isinstance(exc, CatalogError) else "Check PostgreSQL, migrations, and the completed search build"
        print(f"Search verification failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line entry points for catalog setup, import, and independent checks."""

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from app.catalog import (
    ROOT,
    CatalogError,
    apply_migrations,
    connect_database,
    create_database,
    import_catalog,
    validated_source,
    verify_catalog,
)


def main(command):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=f"{command.capitalize()} the PostgreSQL commerce catalog"
    )
    if command == "init":
        parser.add_argument(
            "--create-database",
            action="store_true",
            help="Create the configured dedicated database if missing",
        )
    else:
        parser.add_argument(
            "--etl-output-dir",
            default=os.getenv(
                "CATALOG_ETL_OUTPUT_DIR", "../conversational_commerce_etl/data/processed"
            ),
        )
        parser.add_argument(
            "--manifest",
            help="Import/verify a specific completed run manifest instead of current.json",
        )
        if command == "import":
            parser.add_argument(
                "--check-only",
                action="store_true",
                help="Validate the source without connecting to PostgreSQL",
            )
            parser.add_argument(
                "--stage",
                action="store_true",
                help="Import without changing the serving catalog; publish with build_search_index.py --catalog-import-id",
            )
    args = parser.parse_args()
    database_url = os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
    try:
        if command == "init":
            created = (
                create_database(database_url, os.getenv("CATALOG_ADMIN_DATABASE_URL"))
                if args.create_database
                else False
            )
            with connect_database(database_url) as db:
                apply_migrations(db)
            result = {"status": "ready", "database_created": created, "schema": "commerce"}
        else:
            output = Path(args.etl_output_dir).expanduser()
            if not output.is_absolute():
                output = ROOT / output
            manifest = Path(args.manifest).expanduser() if args.manifest else None
            if manifest is not None and not manifest.is_absolute():
                manifest = ROOT / manifest
            with validated_source(output, manifest) as source:
                if command == "import" and args.check_only:
                    result = {
                        "status": "validated",
                        "run_id": source.manifest["run_id"],
                        "products": source.count,
                    }
                else:
                    with connect_database(database_url) as db:
                        if command == "import":
                            result = import_catalog(db, source, activate=not args.stage)
                            if args.stage:
                                result["activation"] = "staged"
                        else:
                            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                            count = verify_catalog(db, source)
                            result = {
                                "status": "verified",
                                "run_id": source.manifest["run_id"],
                                "products": count,
                            }
        print(json.dumps(result, indent=2))
        return 0
    except psycopg.Error as exc:
        # Connection strings may contain secrets. Never print the DSN or raw exception.
        print(
            f"Catalog {command} failed: PostgreSQL error (SQLSTATE {exc.sqlstate or 'connection'}). "
            "Check connectivity, permissions, and run scripts/init_catalog.py first.",
            file=sys.stderr,
        )
        return 1
    except (CatalogError, OSError, ValueError, KeyError, TypeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, CatalogError)
            else "Invalid or unreadable ETL input/configuration"
        )
        print(
            f"Catalog {command} failed: {message}. Catalog publication was not changed.",
            file=sys.stderr,
        )
        return 1

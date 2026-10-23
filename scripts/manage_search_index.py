"""Preview/remove inactive embedding generations or maintain search indexes."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg
from dotenv import load_dotenv

from app.catalog import ROOT, CatalogError, connect_database
from app.search_maintenance import maintain_indexes, remove_builds


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prune = commands.add_parser(
        "prune", help="Preview/prune inactive generations; always protect the active build"
    )
    prune.add_argument(
        "--keep-ready",
        type=int,
        default=1,
        help="Newest inactive ready builds to retain in addition to the active build",
    )
    prune.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Also select unfinished generations, otherwise preserved for resume",
    )
    prune.add_argument(
        "--apply",
        action="store_true",
        help="Execute the displayed retention policy instead of dry run",
    )
    delete = commands.add_parser("delete", help="Preview/delete explicitly named inactive builds")
    delete.add_argument("--build-id", action="append", required=True)
    delete.add_argument("--apply", action="store_true")
    commands.add_parser(
        "reindex", help="Rebuild search/catalog indexes concurrently and refresh planner statistics"
    )
    commands.add_parser(
        "vacuum", help="Reclaim deleted-vector storage for reuse without VACUUM FULL"
    )
    args = parser.parse_args()
    try:
        with connect_database(
            os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce"),
            autocommit=True,
        ) as db:
            if args.command == "prune":
                result = remove_builds(
                    db,
                    keep_ready=args.keep_ready,
                    include_incomplete=args.include_incomplete,
                    apply=args.apply,
                )
            elif args.command == "delete":
                result = remove_builds(db, build_ids=args.build_id, apply=args.apply)
            else:
                result = maintain_indexes(db, args.command)
        print(json.dumps(result, indent=2))
        return 0
    except (CatalogError, psycopg.Error, OSError, ValueError) as exc:
        message = (
            str(exc)
            if isinstance(exc, CatalogError)
            else "Check PostgreSQL permissions, migrations, and index health"
        )
        print(f"Search maintenance failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

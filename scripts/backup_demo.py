"""Create a coordinated recovery bundle while the API is stopped; never run ETL/models."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psycopg.conninfo import conninfo_to_dict

from app.catalog import connect_database
from app.config import ROOT, Settings
from app.conversation_maintenance import backup_sqlite, offline_lock
from app.embedding_artifacts import artifact_root, build_directory
from app.search_maintenance import maintenance_lock


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != 1 or not manifest.get("files"):
        raise ValueError("Invalid backup manifest")
    for name, expected in manifest["files"].items():
        path = directory / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(directory)
            or digest(path) != expected
        ):
            raise ValueError("Backup file is missing, unsafe, or differs from its checksum")
    return {"verified_files": len(manifest["files"]), "created_at": manifest["created_at"]}


def postgres_environment(url):
    mapping = {
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "dbname": "PGDATABASE",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "service": "PGSERVICE",
        "passfile": "PGPASSFILE",
        "application_name": "PGAPPNAME",
    }
    params = conninfo_to_dict(url)
    if params.keys() - mapping.keys():
        raise ValueError("Backup connection contains unsupported libpq options")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update({mapping[key]: value for key, value in params.items()})
    env.setdefault("PGCONNECT_TIMEOUT", "5")
    return env


def backup(output):
    output = Path(output).resolve()
    artifacts = artifact_root().resolve()
    if output.is_relative_to(artifacts) or artifacts.is_relative_to(output):
        raise ValueError("Backup destination must be separate from serving artifacts")
    database = Path(Settings().database)
    database = database if database.is_absolute() else ROOT / database
    if not database.is_file():
        raise ValueError("Conversation database does not exist")
    url = os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
    env = postgres_environment(url)
    pg_dump = os.getenv("PG_DUMP_BIN", "pg_dump")
    if not shutil.which(pg_dump):
        raise ValueError("Install a compatible pg_dump or set PG_DUMP_BIN")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    # No manifest is written until all components complete. Incomplete bundles
    # remain for inspection; no automatic deletion or destructive replacement.
    with offline_lock(database), connect_database(url, autocommit=True) as db:
        with maintenance_lock(db):
            builds = db.execute(
                "SELECT build_id,status FROM commerce.search_builds ORDER BY build_id"
            ).fetchall()
            backup_sqlite(database, output / "conversations.sqlite3")
            result = subprocess.run(
                [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--file",
                    str(output / "catalog.dump"),
                ],
                env=env,
                capture_output=True,
                timeout=1800,
            )
            if result.returncode:
                raise ValueError(
                    "Catalog backup failed; verify PostgreSQL tool compatibility and permissions"
                )
            for build_id, status in builds:
                source = build_directory(build_id)
                if not source.exists():
                    if status == "ready":
                        raise ValueError("A ready build has missing embedding artifacts")
                    continue
                if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
                    raise ValueError("Backup refuses symlinked artifact trees")
                shutil.copytree(source, output / "embeddings" / build_id)
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "qdrant_recovery": "Rebuild from restored PostgreSQL metadata and Parquet vectors; no embedding inference required",
        "build_ids": [row[0] for row in builds],
        "files": {
            str(path.relative_to(output)): digest(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return verify(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", help="New private directory; API must be stopped")
    group.add_argument("--verify", help="Read and checksum an existing bundle only")
    args = parser.parse_args()
    try:
        result = verify(args.verify) if args.verify else backup(args.output)
    except Exception:
        # Connection URLs and subprocess output can contain credentials.
        print(
            "Backup/verification failed. No complete new bundle should be assumed; check paths, available space, tools, locks and dependency access.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

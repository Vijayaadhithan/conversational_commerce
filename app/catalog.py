"""Validated, versioned PostgreSQL imports of normalized ETL subsets.

This module does not load chat/model code or alter conversation storage.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

import jsonschema
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema/catalog-product-v1.schema.json"
MIGRATIONS = ROOT / "migrations/catalog"
SCHEMA_VERSION = "1.0.0"
LOCK_ID = 71482743619401
MAX_LINE_BYTES = 8 * 1024 * 1024


class CatalogError(ValueError):
    """A catalog/configuration/integrity error safe to show without credentials."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_number(value):
    raise CatalogError("Non-finite JSON number")


def _float(value):
    number = float(value)
    if not Decimal(value).is_finite() or number in (float("inf"), float("-inf")):
        raise CatalogError("Non-finite JSON number")
    return number


def strict_json(data):
    return json.loads(data, object_pairs_hook=_unique_object,
                      parse_constant=_invalid_number, parse_float=_float)


def _integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        raise CatalogError(f"Invalid {label}")
    return value


def _digest(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CatalogError(f"Invalid {label}")
    return value


def _check_pg_json(value):
    if isinstance(value, str):
        value.encode("utf-8")  # Reject unpaired surrogates, even in escaped JSON.
        if "\x00" in value:
            raise CatalogError("NUL is not supported by PostgreSQL JSONB")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_pg_json(key)
            _check_pg_json(item)
    elif isinstance(value, list):
        for item in value:
            _check_pg_json(item)


def _read_rows(stream):
    ordinal = 0
    while line := stream.readline(MAX_LINE_BYTES + 1):
        ordinal += 1
        if len(line) > MAX_LINE_BYTES:
            raise CatalogError(f"Subset row {ordinal} exceeds 8 MiB")
        try:
            # JSONB cannot represent NUL or unpaired Unicode surrogates.
            record = strict_json(line)
            _check_pg_json(record)
        except (ValueError, UnicodeError) as exc:
            raise CatalogError(f"Invalid JSON at subset row {ordinal}") from exc
        yield ordinal, line, record


def _validate_record(record, validator, sources):
    try:
        validator.validate(record)
    except jsonschema.ValidationError as exc:
        raise CatalogError("Product does not match the pinned ETL schema") from exc
    if record["product_id"] != "amazon:" + record["parent_asin"]:
        raise CatalogError("Product ID and parent ASIN disagree")
    if not record["title"].strip():
        raise CatalogError("Product title is blank")
    price = record["price"]
    if (price["parse_status"] != "valid" or price["status"] != "historical"
            or price["currency"] != "USD" or price["amount"] is None
            or Decimal(price["amount"]) <= 0):
        raise CatalogError("Subset requires a positive historical USD price")
    if (record["category"]["domain"] not in {"beauty", "fashion"}
            or record["category"]["family"] in {"", "unknown"}
            or not record["quality"]["eligible_for_subset"]
            or not any(text.strip() for values in record["content"].values() for text in values)):
        raise CatalogError("Product is not eligible for the recommendation subset")
    if set(record["quality"]["conflicting_fields"]) & {"price", "family", "brand", "color", "size", "material"}:
        raise CatalogError("Subset contains a product with critical attribute conflicts")
    if record["brand"] is not None and not isinstance(record["brand"]["value"], str):
        raise CatalogError("Brand claim must be text")
    for key in ("description", "features"):
        if len(record["content"][key]) != len(record["content_source_fields"][key]):
            raise CatalogError("Content and source-field references disagree")
    selected = [item for item in record["observations"] if item["selected"]]
    if len(selected) != 1 or any(selected[0][key] != record["selected_observation"][key]
                                 for key in ("source", "row", "record_sha256")):
        raise CatalogError("Selected observation and provenance disagree")
    for item in record["observations"]:
        if item["source"] not in sources or item["row"] > sources[item["source"]]:
            raise CatalogError("Observation does not reference a valid manifest source row")


@dataclass
class CatalogSource:
    manifest: dict
    import_id: str
    stream: object
    count: int

    def rows(self):
        self.stream.seek(0)
        return _read_rows(self.stream)


@contextmanager
def validated_source(output_dir, manifest_path=None):
    """Pin one manifest/file descriptor, validate all rows, and keep memory bounded."""
    output = Path(output_dir).expanduser().resolve()
    if manifest_path is None:
        pointer = strict_json((output / "current.json").read_bytes())
        path = (output / pointer["manifest"]).resolve()
        if not path.is_relative_to(output):
            raise CatalogError("Manifest path escapes the ETL output directory")
    else:
        pointer = None
        path = Path(manifest_path).expanduser().resolve()
    manifest_bytes = path.read_bytes()
    manifest = strict_json(manifest_bytes)
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise CatalogError("Unsupported ETL schema version")
    if pointer is not None and pointer["run_id"] != manifest["run_id"]:
        raise CatalogError("ETL pointer and manifest run IDs disagree")
    if not isinstance(manifest["run_id"], str) or not re.fullmatch(r"[0-9a-f]{20}", manifest["run_id"]):
        raise CatalogError("Invalid ETL run ID")
    schema_bytes = SCHEMA_PATH.read_bytes()
    if hashlib.sha256(schema_bytes).hexdigest() != manifest["schema_sha256"]:
        raise CatalogError("Manifest schema checksum differs from the pinned local schema")
    validator = jsonschema.Draft202012Validator(strict_json(schema_bytes))
    expected = manifest["outputs"]["subset.jsonl"]
    _digest(expected["sha256"], "subset checksum")
    expected_bytes = _integer(expected["bytes"], "subset size", minimum=0)
    expected_count = _integer(manifest["counts"]["subset_rows"], "subset count", minimum=0)
    if expected_count != _integer(manifest["subset_size"], "requested subset size"):
        # A documented ETL shortfall is valid; only counts above the request fail.
        if expected_count > manifest["subset_size"]:
            raise CatalogError("Subset count exceeds requested size")
    sources = {}
    for item in manifest["sources"]:
        if not isinstance(item["name"], str) or item["name"] in sources:
            raise CatalogError("Invalid or duplicate manifest source")
        sources[item["name"]] = _integer(item["expected_rows"], "source row count")
    subset_path = (path.parent / "subset.jsonl").resolve()
    if not subset_path.is_relative_to(path.parent):
        raise CatalogError("Subset path escapes the run directory")
    with subset_path.open("rb") as stream:
        digest, size, ids = hashlib.sha256(), 0, set()
        for ordinal, line, record in _read_rows(stream):
            _validate_record(record, validator, sources)
            if record["product_id"] in ids:
                raise CatalogError("Duplicate subset product ID")
            ids.add(record["product_id"])
            digest.update(line)
            size += len(line)
        if (len(ids), size, digest.hexdigest()) != (expected_count, expected_bytes, expected["sha256"]):
            raise CatalogError("Subset count, byte size, or SHA-256 differs from manifest")
        yield CatalogSource(manifest, hashlib.sha256(manifest_bytes).hexdigest(), stream, len(ids))


def connect_database(database_url, **kwargs):
    return psycopg.connect(database_url, connect_timeout=5,
                           application_name="conversational_commerce_catalog", **kwargs)


def create_database(database_url, admin_url=None):
    """Create only the explicitly selected catalog DB; preserve existing databases."""
    options = conninfo_to_dict(database_url)
    name = options.get("dbname")
    if not name or name in {"postgres", "template0", "template1"}:
        raise CatalogError("Choose a dedicated catalog database name")
    admin = admin_url or make_conninfo(database_url, dbname="postgres")
    with connect_database(admin, autocommit=True) as db:
        if db.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return False
        try:
            db.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            return False
    return True


def apply_migrations(db):
    with db.transaction():
        db.execute("SET LOCAL lock_timeout = '5s'")
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        if db.execute("SELECT current_database()").fetchone()[0] in {"postgres", "template0", "template1"}:
            raise CatalogError("Catalog migrations require a dedicated database")
        db.execute("CREATE SCHEMA IF NOT EXISTS commerce")
        db.execute("CREATE TABLE IF NOT EXISTS commerce.schema_migrations (version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        applied = dict(db.execute("SELECT version, sha256 FROM commerce.schema_migrations").fetchall())
        migrations = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
        if set(applied) - {path.name for path in migrations}:
            raise CatalogError("Database has migrations unsupported by this checkout")
        for path in migrations:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if path.name in applied:
                if applied[path.name] != digest:
                    raise CatalogError("An applied migration differs from the local file")
                continue
            db.execute(data.decode("utf-8"))
            db.execute("INSERT INTO commerce.schema_migrations(version, sha256) VALUES (%s, %s)", (path.name, digest))


def _check_schema(db):
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        row = db.execute("SELECT sha256 FROM commerce.schema_migrations WHERE version = %s", (path.name,)).fetchone()
        if row != (hashlib.sha256(path.read_bytes()).hexdigest(),):
            raise CatalogError("Initialize the catalog schema with scripts/init_catalog.py")


def _check_file_hash(source, digest, count):
    if count != source.count or digest.hexdigest() != source.manifest["outputs"]["subset.jsonl"]["sha256"]:
        raise CatalogError("Subset changed after preflight validation")


def verify_catalog(db, source, require_active=True):
    """Compare every stored record and typed filter field against the source file."""
    meta = db.execute("SELECT manifest, product_count FROM commerce.catalog_imports WHERE import_id = %s", (source.import_id,)).fetchone()
    if meta != (source.manifest, source.count):
        raise CatalogError("Stored import metadata does not match the manifest")
    if require_active:
        if db.execute("SELECT import_id FROM commerce.catalog_active WHERE singleton").fetchone() != (source.import_id,):
            raise CatalogError("This manifest is not the active catalog")
    digest, count = hashlib.sha256(), 0
    with db.cursor(name="verify_catalog_records") as cursor:
        cursor.itersize = 250
        cursor.execute("""SELECT ordinal, document, source_line_sha256, product_id, parent_asin,
            title, domain, family, brand, historical_price, currency, attributes, source_name
            FROM commerce.catalog_products WHERE import_id = %s ORDER BY ordinal""", (source.import_id,))
        stored_rows = iter(cursor)
        for ordinal, line, record in source.rows():
            price = record["price"]["amount"]
            expected = (ordinal, record, hashlib.sha256(line).hexdigest(), record["product_id"],
                        record["parent_asin"], record["title"], record["category"]["domain"],
                        record["category"]["family"], record["brand"]["value"] if record["brand"] else None,
                        Decimal(price) if price is not None else None, record["price"]["currency"],
                        record["attributes"], record["selected_observation"]["source"])
            if next(stored_rows, None) != expected:
                raise CatalogError(f"Stored product differs from the source at subset row {ordinal}")
            digest.update(line)
            count += 1
        if next(stored_rows, None) is not None:
            raise CatalogError("Database contains extra products for this import")
    _check_file_hash(source, digest, count)
    return count


def import_catalog(db, source):
    """COPY into staging, publish an immutable snapshot and active pointer atomically."""
    with db.transaction():
        db.execute("SET LOCAL lock_timeout = '5s'")
        if not db.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_ID,)).fetchone()[0]:
            raise CatalogError("Another catalog import or migration is in progress; retry later")
        _check_schema(db)
        exists = db.execute("SELECT 1 FROM commerce.catalog_imports WHERE import_id = %s", (source.import_id,)).fetchone()
        if exists:
            verify_catalog(db, source, require_active=False)
        else:
            db.execute("CREATE TEMP TABLE catalog_stage (LIKE commerce.catalog_products INCLUDING GENERATED INCLUDING CONSTRAINTS INCLUDING INDEXES) ON COMMIT DROP")
            digest, count = hashlib.sha256(), 0
            with db.cursor().copy("COPY catalog_stage(import_id, ordinal, document, source_line_sha256) FROM STDIN") as copy:
                for ordinal, line, record in source.rows():
                    copy.write_row((source.import_id, ordinal, Jsonb(record), hashlib.sha256(line).hexdigest()))
                    digest.update(line)
                    count += 1
            _check_file_hash(source, digest, count)
            manifest = source.manifest
            db.execute("""INSERT INTO commerce.catalog_imports
                (import_id, run_id, schema_version, subset_sha256, schema_sha256, product_count, manifest)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (source.import_id, manifest["run_id"], SCHEMA_VERSION,
                 manifest["outputs"]["subset.jsonl"]["sha256"], manifest["schema_sha256"], count, Jsonb(manifest)))
            db.execute("""INSERT INTO commerce.catalog_products(import_id, ordinal, document, source_line_sha256)
                SELECT import_id, ordinal, document, source_line_sha256 FROM catalog_stage""")
            verify_catalog(db, source, require_active=False)
        active = db.execute("SELECT import_id FROM commerce.catalog_active WHERE singleton").fetchone()
        if active != (source.import_id,):
            db.execute("""INSERT INTO commerce.catalog_active(singleton, import_id) VALUES (true, %s)
                ON CONFLICT (singleton) DO UPDATE SET import_id = EXCLUDED.import_id,
                activated_at = CURRENT_TIMESTAMP""", (source.import_id,))
        return {"status": "reused" if exists else "imported", "run_id": source.manifest["run_id"],
                "import_id": source.import_id, "products": source.count}

"""Catalog contract tests; PostgreSQL tests use a separate scratch database."""

import hashlib
import json
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app import catalog


def product(asin="B000000001", price="12.30"):
    observation = {"source": "Fixture", "row": 1, "record_sha256": "a" * 64}
    return {
        "schema_version": "1.0.0", "product_id": "amazon:" + asin, "parent_asin": asin,
        "title": "Example moisturizer", "category": {"domain": "beauty", "family": "skin_care",
            "basis": "title_rule", "evidence": {"field": "/title", "text": "Example moisturizer"}},
        "brand": None, "store": None,
        "price": {"amount": price, "currency": "USD", "status": "historical",
            "parse_status": "valid", "field": "/price", "observed_value": price},
        "categories": [], "attributes": {"fragrance_free": None, "skin_type": None},
        "content": {"description": ["An explicit listing description."], "features": []},
        "content_source_fields": {"description": ["/description/0"], "features": []},
        "image_urls": [], "rating": {"average": None, "count": None}, "availability": None,
        "selected_observation": observation,
        "observations": [{**observation, "selected": True}],
        "quality": {"score": 5, "warnings": [], "conflicting_fields": [], "eligible_for_subset": True},
    }


def make_source(root, records=None):
    records = records if records is not None else [product()]
    data = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in records)
    run_id = hashlib.sha256(data).hexdigest()[:20]
    run = root / "runs" / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "subset.jsonl").write_bytes(data)
    manifest = {"schema_version": "1.0.0", "run_id": run_id, "subset_size": len(records),
        "schema_sha256": hashlib.sha256(catalog.SCHEMA_PATH.read_bytes()).hexdigest(),
        "sources": [{"name": "Fixture", "expected_rows": 10}],
        "counts": {"subset_rows": len(records)},
        "outputs": {"subset.jsonl": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}
    (run / "manifest.json").write_text(json.dumps(manifest))
    (root / "current.json").write_text(json.dumps({"run_id": run_id, "manifest": f"runs/{run_id}/manifest.json"}))
    return root, run, manifest


def test_source_preserves_unknowns_and_exact_price(tmp_path):
    make_source(tmp_path)
    with catalog.validated_source(tmp_path) as source:
        record = next(source.rows())[2]
        assert source.count == 1
        assert record["price"]["amount"] == "12.30"
        assert record["brand"] is None and record["attributes"]["fragrance_free"] is None


@pytest.mark.parametrize("change", [
    lambda p: p.update(product_id="amazon:B000000002"),
    lambda p: p["price"].update(amount="0.00"),
    lambda p: p["price"].update(currency=None),
    lambda p: p["category"].update(domain="unknown"),
    lambda p: p["quality"].update(eligible_for_subset=False),
    lambda p: p["quality"].update(conflicting_fields=["price"]),
    lambda p: p["observations"][0].update(selected=False),
    lambda p: p["selected_observation"].update(row=11),
    lambda p: p["content_source_fields"].update(description=[]),
    lambda p: p.update(title="\x00"),
])
def test_reject_invalid_or_ineligible_rows(tmp_path, change):
    record = product()
    change(record)
    make_source(tmp_path, [record])
    with pytest.raises(catalog.CatalogError):
        with catalog.validated_source(tmp_path):
            pass


def test_invalid_unicode_rejected(tmp_path):
    root, run, manifest = make_source(tmp_path)
    (run / "subset.jsonl").write_bytes(b'{"title":"\\ud800"}\n')
    with pytest.raises(catalog.CatalogError):
        with catalog.validated_source(root): pass


def test_literal_unicode_escape_text_is_preserved(tmp_path):
    row = product()
    row["content"]["description"] = [r"A literal \u0000 escape in listing text"]
    make_source(tmp_path, [row])
    with catalog.validated_source(tmp_path) as source:
        assert next(source.rows())[2] == row


def test_checksum_and_schema_mismatch(tmp_path):
    root, run, manifest = make_source(tmp_path)
    with (run / "subset.jsonl").open("ab") as stream: stream.write(b" ")
    with pytest.raises(catalog.CatalogError):
        with catalog.validated_source(root): pass
    root, run, manifest = make_source(tmp_path)
    manifest["schema_sha256"] = "0" * 64
    (run / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(catalog.CatalogError, match="schema checksum"):
        with catalog.validated_source(root): pass


def test_duplicate_ids_and_path_escape_rejected(tmp_path):
    make_source(tmp_path, [product(), product()])
    with pytest.raises(catalog.CatalogError, match="Duplicate subset"):
        with catalog.validated_source(tmp_path): pass
    (tmp_path / "current.json").write_text(json.dumps({"run_id": "a" * 20, "manifest": "../escape.json"}))
    with pytest.raises(catalog.CatalogError, match="escapes"):
        with catalog.validated_source(tmp_path): pass


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'])
def test_nonstandard_json_rejected(data):
    with pytest.raises(catalog.CatalogError): catalog.strict_json(data)


def test_cli_redacts_connection_errors(monkeypatch, capsys):
    from app import catalog_cli
    monkeypatch.setattr("sys.argv", ["init_catalog.py"])
    monkeypatch.setenv("CATALOG_DATABASE_URL", "postgresql://user:private-password@localhost/catalog")
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("postgresql://user:private-password@localhost/catalog")
    monkeypatch.setattr(catalog_cli, "connect_database", fail)
    assert catalog_cli.main("init") == 1
    assert "private-password" not in capsys.readouterr().err


@pytest.fixture(scope="session")
def scratch_database():
    admin_url = os.getenv("CATALOG_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("Set CATALOG_TEST_ADMIN_URL to enable isolated PostgreSQL integration tests")
    name = "commerce_catalog_test_" + uuid4().hex[:16]
    url = make_conninfo(admin_url, dbname=name)
    catalog.create_database(url, admin_url)
    try:
        yield url
    finally:
        with catalog.connect_database(admin_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def pg(scratch_database):
    # Only reset the randomly named database created by scratch_database above.
    with catalog.connect_database(scratch_database) as db:
        db.execute("DROP SCHEMA IF EXISTS commerce CASCADE")
        db.commit()
        catalog.apply_migrations(db)
        yield db


def test_postgres_roundtrip_idempotence_and_filter_columns(pg, tmp_path):
    records = [product(), product("B000000002", "999999999999999.99")]
    records[1]["brand"] = {"value": "O'Reilly", "field": "/details/Brand", "status": "listing_claim"}
    records[1]["attributes"]["fragrance_free"] = {"value": True, "field": "/details/Scent", "status": "listing_claim"}
    make_source(tmp_path, records)
    with catalog.validated_source(tmp_path) as source:
        assert catalog.import_catalog(pg, source)["status"] == "imported"
        assert catalog.import_catalog(pg, source)["status"] == "reused"
        assert catalog.verify_catalog(pg, source) == 2
    assert pg.execute("SELECT count(*) FROM commerce.catalog_imports").fetchone() == (1,)
    assert pg.execute("SELECT product_id FROM commerce.active_products WHERE historical_price <= %s", ("20.00",)).fetchall() == [("amazon:B000000001",)]
    assert pg.execute("SELECT count(*) FROM commerce.active_products WHERE attributes #>> '{fragrance_free,value}' = 'true'").fetchone() == (1,)
    assert pg.execute("SELECT count(*) FROM commerce.active_products WHERE attributes -> 'fragrance_free' = 'null'::jsonb").fetchone() == (1,)


def test_new_snapshot_removes_stale_products_from_active_view(pg, tmp_path):
    make_source(tmp_path, [product(), product("B000000002")])
    with catalog.validated_source(tmp_path) as source: catalog.import_catalog(pg, source)
    make_source(tmp_path, [product("B000000002", "8.20")])
    with catalog.validated_source(tmp_path) as source: catalog.import_catalog(pg, source)
    assert pg.execute("SELECT product_id, historical_price::text FROM commerce.active_products").fetchall() == [("amazon:B000000002", "8.20")]
    assert pg.execute("SELECT count(*) FROM commerce.catalog_products").fetchone() == (3,)
    assert pg.execute("SELECT count(*) FROM commerce.catalog_imports").fetchone() == (2,)


def test_failure_after_insertion_rolls_back_new_snapshot(pg, tmp_path, monkeypatch):
    make_source(tmp_path)
    with catalog.validated_source(tmp_path) as source:
        original = catalog.import_catalog(pg, source)["import_id"]
    make_source(tmp_path, [product("B000000002")])
    def fail(*args, **kwargs): raise catalog.CatalogError("Injected verification failure")
    monkeypatch.setattr(catalog, "verify_catalog", fail)
    with catalog.validated_source(tmp_path) as source, pytest.raises(catalog.CatalogError, match="Injected"):
        catalog.import_catalog(pg, source)
    assert pg.execute("SELECT import_id FROM commerce.catalog_active").fetchone() == (original,)
    assert pg.execute("SELECT count(*) FROM commerce.catalog_imports").fetchone() == (1,)
    assert pg.execute("SELECT product_id FROM commerce.active_products").fetchall() == [("amazon:B000000001",)]


def test_file_mutation_during_import_rolls_back(pg, tmp_path):
    root, run, manifest = make_source(tmp_path)
    with catalog.validated_source(root) as source:
        path = run / "subset.jsonl"
        path.write_bytes(path.read_bytes().replace(b"12.30", b"13.30"))
        with pytest.raises(catalog.CatalogError, match="changed after preflight"):
            catalog.import_catalog(pg, source)
    assert pg.execute("SELECT count(*) FROM commerce.catalog_imports").fetchone() == (0,)
    assert pg.execute("SELECT count(*) FROM commerce.catalog_active").fetchone() == (0,)


def test_verifier_detects_database_drift_and_reimport_refuses_it(pg, tmp_path):
    make_source(tmp_path)
    with catalog.validated_source(tmp_path) as source:
        catalog.import_catalog(pg, source)
        pg.execute("UPDATE commerce.catalog_products SET document = jsonb_set(document, '{title}', '\"Unexpected title\"')")
        with pytest.raises(catalog.CatalogError, match="differs from the source"):
            catalog.verify_catalog(pg, source)
        with pytest.raises(catalog.CatalogError): catalog.import_catalog(pg, source)


def test_migration_checksum_and_import_lock(pg, scratch_database, tmp_path, monkeypatch):
    migration = next(catalog.MIGRATIONS.glob("*.sql"))
    changed = tmp_path / "migrations"
    changed.mkdir()
    (changed / migration.name).write_bytes(migration.read_bytes() + b"\n-- changed\n")
    with monkeypatch.context() as scoped:
        scoped.setattr(catalog, "MIGRATIONS", changed)
        with pytest.raises(catalog.CatalogError, match="migration differs"):
            catalog.apply_migrations(pg)
    make_source(tmp_path / "source")
    with catalog.connect_database(scratch_database) as other:
        other.execute("SELECT pg_advisory_xact_lock(%s)", (catalog.LOCK_ID,))
        with catalog.validated_source(tmp_path / "source") as source:
            with pytest.raises(catalog.CatalogError, match="in progress"):
                catalog.import_catalog(pg, source)

"""Resumable embedding builds with atomic publication and source checks."""

import hashlib
import json

from psycopg.types.json import Jsonb

from app.catalog import CatalogError, LOCK_ID, _check_schema
from app.embedding_text import contract, document_text
from app.embeddings import vector_literal

BUILD_LOCK = LOCK_ID + 1


def build_config(import_id, embedder):
    return {"import_id": import_id, "model": embedder.settings.model,
            "model_digest": embedder.digest, "input_contract": contract()}


def verify_build(db, build_id):
    meta = db.execute("SELECT import_id, config, expected_count FROM commerce.search_builds WHERE build_id=%s", (build_id,)).fetchone()
    if not meta or meta[1]["input_contract"] != contract():
        raise CatalogError("Search build is missing or has a different input contract")
    count, clipped = 0, 0
    with db.cursor(name="verify_search_inputs") as cursor:
        cursor.itersize = 100
        cursor.execute("""SELECT p.document, p.source_line_sha256, d.source_line_sha256,
            d.input_sha256, d.title, d.body, d.input_clipped, public.vector_norm(d.embedding)
            FROM commerce.search_documents d JOIN commerce.catalog_products p
            ON (p.import_id,p.product_id)=(d.import_id,d.product_id)
            WHERE d.build_id=%s ORDER BY d.product_id""", (build_id,))
        for record, source_sha, stored_sha, input_sha, title, body, was_clipped, norm in cursor:
            expected = document_text(record)
            if ((source_sha, expected["sha256"], expected["title"], expected["body"], expected["clipped"])
                    != (stored_sha, input_sha, title, body, was_clipped) or not 0.999 <= norm <= 1.001):
                raise CatalogError("Search build differs from the current catalog or input contract")
            count += 1
            clipped += was_clipped
    if count != meta[2]:
        raise CatalogError("Search build is incomplete")
    return {"products": count, "selected_text_clipped": clipped}


def build_index(db, embedder, progress=print):
    """Use an autocommit connection: each batch commits, publication is atomic."""
    if not db.autocommit:
        raise CatalogError("Embedding builds require an autocommit connection")
    if not db.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK,)).fetchone()[0]:
        raise CatalogError("Another embedding build is running")
    try:
        _check_schema(db)
        active = db.execute("SELECT import_id FROM commerce.catalog_active WHERE singleton").fetchone()
        if not active:
            raise CatalogError("Import the catalog before building embeddings")
        import_id = active[0]
        expected = db.execute("SELECT product_count FROM commerce.catalog_imports WHERE import_id=%s", (import_id,)).fetchone()[0]
        embedder.verify_identity()
        config = build_config(import_id, embedder)
        build_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        db.execute("""INSERT INTO commerce.search_builds(build_id,import_id,config,expected_count)
            VALUES (%s,%s,%s,%s) ON CONFLICT (build_id) DO NOTHING""", (build_id, import_id, Jsonb(config), expected))
        existed = db.execute("SELECT status FROM commerce.search_builds WHERE build_id=%s", (build_id,)).fetchone()[0] == "ready"
        while True:
            rows = db.execute("""SELECT p.product_id,p.document,p.source_line_sha256
                FROM commerce.catalog_products p WHERE p.import_id=%s AND NOT EXISTS
                (SELECT 1 FROM commerce.search_documents d WHERE d.build_id=%s AND d.product_id=p.product_id)
                ORDER BY p.product_id LIMIT %s""", (import_id, build_id, embedder.settings.batch_size)).fetchall()
            if not rows:
                break
            inputs = [document_text(row[1]) for row in rows]
            vectors = embedder.embed([item["input"] for item in inputs])
            with db.transaction():
                with db.cursor().copy("""COPY commerce.search_documents
                    (build_id,import_id,product_id,source_line_sha256,input_sha256,title,body,input_clipped,embedding)
                    FROM STDIN""") as copy:
                    for (product_id, record, source_sha), item, vector in zip(rows, inputs, vectors, strict=True):
                        copy.write_row((build_id, import_id, product_id, source_sha, item["sha256"],
                                        item["title"], item["body"], item["clipped"], vector_literal(vector)))
            completed = db.execute("SELECT count(*) FROM commerce.search_documents WHERE build_id=%s", (build_id,)).fetchone()[0]
            progress(f"Embedded {completed:,}/{expected:,} products", flush=True)
        embedder.verify_identity(config["model_digest"])
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='5s'")
            db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            verified = verify_build(db, build_id)
            if db.execute("SELECT import_id FROM commerce.catalog_active WHERE singleton").fetchone() != (import_id,):
                raise CatalogError("Catalog changed during embedding; rerun for the active snapshot")
            db.execute("UPDATE commerce.search_builds SET status='ready',completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP) WHERE build_id=%s", (build_id,))
            db.execute("""INSERT INTO commerce.search_active(singleton,build_id) VALUES (true,%s)
                ON CONFLICT (singleton) DO UPDATE SET build_id=EXCLUDED.build_id,activated_at=CURRENT_TIMESTAMP
                WHERE commerce.search_active.build_id<>EXCLUDED.build_id""", (build_id,))
        return {"status": "reused" if existed else "ready", "build_id": build_id, **verified}
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK,))

"""Resumable embedding builds with atomic publication and source checks."""

import hashlib
import json
import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.catalog import LOCK_ID, CatalogError, _check_schema
from app.embedding_text import contract, document_text
from app.embeddings import vector_literal

BUILD_LOCK = LOCK_ID + 1


def build_config(import_id, embedder):
    return {
        "import_id": import_id,
        "model": embedder.settings.model,
        "model_digest": embedder.digest,
        "input_contract": contract(),
    }


def verify_build(db, build_id):
    meta = db.execute(
        "SELECT import_id, config, expected_count FROM commerce.search_builds WHERE build_id=%s",
        (build_id,),
    ).fetchone()
    if not meta or meta[1]["input_contract"] != contract():
        raise CatalogError("Search build is missing or has a different input contract")
    count, clipped = 0, 0
    with db.cursor(name="verify_search_inputs") as cursor:
        cursor.itersize = 100
        cursor.execute(
            """SELECT p.document, p.source_line_sha256, d.source_line_sha256,
            d.input_sha256, d.title, d.body, d.input_clipped, public.vector_norm(d.embedding)
            FROM commerce.search_documents d JOIN commerce.catalog_products p
            ON (p.import_id,p.product_id)=(d.import_id,d.product_id)
            WHERE d.build_id=%s ORDER BY d.product_id""",
            (build_id,),
        )
        for record, source_sha, stored_sha, input_sha, title, body, was_clipped, norm in cursor:
            expected = document_text(record)
            if (
                source_sha,
                expected["sha256"],
                expected["title"],
                expected["body"],
                expected["clipped"],
            ) != (stored_sha, input_sha, title, body, was_clipped) or not 0.999 <= norm <= 1.001:
                raise CatalogError(
                    "Search build differs from the current catalog or input contract"
                )
            count += 1
            clipped += was_clipped
    if count != meta[2]:
        raise CatalogError("Search build is incomplete")
    return {"products": count, "selected_text_clipped": clipped}


def _select_build(db, config, reembed_all, reembed_ids, resume_build):
    if resume_build:
        if reembed_all or reembed_ids:
            raise CatalogError("Resume cannot be combined with refresh options")
        row = db.execute(
            "SELECT config FROM commerce.search_builds WHERE build_id=%s", (resume_build,)
        ).fetchone()
        if not row or any(row[0].get(key) != value for key, value in config.items()):
            raise CatalogError(
                "Resume build does not match the active catalog, model, and input contract"
            )
        return resume_build, row[0]
    if reembed_all and reembed_ids:
        raise CatalogError("Choose either all products or selected products for re-embedding")
    if reembed_all or reembed_ids:
        # A distinct generation keeps the serving generation untouched during refresh.
        config = {
            **config,
            "refresh": {"id": uuid4().hex, "all": reembed_all, "product_ids": reembed_ids},
        }
    else:
        # Preserve a deliberately refreshed serving build instead of reverting to the original.
        row = db.execute(
            """SELECT b.build_id,b.config FROM commerce.search_builds b
            LEFT JOIN commerce.search_active a USING(build_id)
            WHERE b.config @> %s AND b.status='ready'
            ORDER BY (a.build_id IS NOT NULL) DESC,b.completed_at DESC,b.build_id LIMIT 1""",
            (Jsonb(config),),
        ).fetchone()
        if row:
            return row
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(), config


def _cached_vectors(db, inputs, config):
    if not inputs:
        return {}
    return dict(
        db.execute(
            """SELECT DISTINCT ON (d.input_sha256) d.input_sha256,d.embedding::text
        FROM commerce.search_documents d JOIN commerce.search_builds b USING(build_id)
        WHERE d.input_sha256=ANY(%s) AND b.status='ready'
          AND b.config->>'model_digest'=%s AND b.config->'input_contract'=%s
          AND public.vector_norm(d.embedding) BETWEEN 0.999 AND 1.001
        ORDER BY d.input_sha256,b.completed_at DESC,b.build_id""",
            (
                [item["sha256"] for item in inputs],
                config["model_digest"],
                Jsonb(config["input_contract"]),
            ),
        ).fetchall()
    )


def build_index(
    db,
    embedder,
    progress=print,
    *,
    reembed_all=False,
    reembed_ids=None,
    resume_build=None,
    catalog_import_id=None,
):
    """Use an autocommit connection: each batch commits, publication is atomic."""
    if not db.autocommit:
        raise CatalogError("Embedding builds require an autocommit connection")
    if not db.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK,)).fetchone()[0]:
        raise CatalogError("Another embedding build is running")
    try:
        _check_schema(db)
        active = db.execute(
            "SELECT import_id FROM commerce.catalog_active WHERE singleton"
        ).fetchone()
        if not active and not catalog_import_id:
            raise CatalogError("Import the catalog before building embeddings")
        import_id = catalog_import_id or active[0]
        imported = db.execute(
            "SELECT product_count FROM commerce.catalog_imports WHERE import_id=%s", (import_id,)
        ).fetchone()
        if not imported:
            raise CatalogError("Requested catalog import does not exist")
        expected = imported[0]
        embedder.verify_identity()
        config = build_config(import_id, embedder)
        reembed_ids = sorted(set(reembed_ids or []))
        if any(not re.fullmatch(r"amazon:[A-Z0-9]{10}", item) for item in reembed_ids):
            raise CatalogError("Re-embedding requires valid product IDs")
        if reembed_ids:
            found = db.execute(
                "SELECT product_id FROM commerce.catalog_products WHERE import_id=%s AND product_id=ANY(%s)",
                (import_id, reembed_ids),
            ).fetchall()
            if len(found) != len(reembed_ids):
                raise CatalogError("Selected re-embedding IDs are missing from the active catalog")
        build_id, config = _select_build(db, config, reembed_all, reembed_ids, resume_build)
        refresh = config.get("refresh", {})
        forced_ids = set(refresh.get("product_ids", []))
        db.execute(
            """INSERT INTO commerce.search_builds(build_id,import_id,config,expected_count)
            VALUES (%s,%s,%s,%s) ON CONFLICT (build_id) DO NOTHING""",
            (build_id, import_id, Jsonb(config), expected),
        )
        existed = (
            db.execute(
                "SELECT status FROM commerce.search_builds WHERE build_id=%s", (build_id,)
            ).fetchone()[0]
            == "ready"
        )
        generated, copied = 0, 0
        completed = db.execute(
            "SELECT count(*) FROM commerce.search_documents WHERE build_id=%s", (build_id,)
        ).fetchone()[0]
        last_product_id = ""
        progress(f"Search build {build_id}; resume with --resume-build {build_id}", flush=True)
        # Never repair a published generation in place; final verification rejects drift.
        while not existed:
            rows = db.execute(
                """SELECT p.product_id,p.document,p.source_line_sha256
                FROM commerce.catalog_products p WHERE p.import_id=%s AND p.product_id>%s AND NOT EXISTS
                (SELECT 1 FROM commerce.search_documents d WHERE d.build_id=%s AND d.product_id=p.product_id)
                ORDER BY p.product_id LIMIT %s""",
                (import_id, last_product_id, build_id, embedder.settings.batch_size),
            ).fetchall()
            if not rows:
                break
            inputs = [document_text(row[1]) for row in rows]
            reusable = [
                item
                for row, item in zip(rows, inputs, strict=True)
                if not refresh.get("all") and row[0] not in forced_ids
            ]
            cache = _cached_vectors(db, reusable, config)
            missing = [
                index
                for index, (row, item) in enumerate(zip(rows, inputs, strict=True))
                if refresh.get("all") or row[0] in forced_ids or item["sha256"] not in cache
            ]
            fresh = embedder.embed([inputs[index]["input"] for index in missing]) if missing else []
            vectors = {
                index: vector_literal(vector) for index, vector in zip(missing, fresh, strict=True)
            }
            with db.transaction():
                with db.cursor().copy("""COPY commerce.search_documents
                    (build_id,import_id,product_id,source_line_sha256,input_sha256,title,body,input_clipped,embedding,embedding_reused)
                    FROM STDIN""") as copy:
                    for index, ((product_id, _record, source_sha), item) in enumerate(
                        zip(rows, inputs, strict=True)
                    ):
                        reused = index not in vectors
                        vector = cache[item["sha256"]] if reused else vectors[index]
                        copy.write_row(
                            (
                                build_id,
                                import_id,
                                product_id,
                                source_sha,
                                item["sha256"],
                                item["title"],
                                item["body"],
                                item["clipped"],
                                vector,
                                reused,
                            )
                        )
            generated += len(missing)
            copied += len(rows) - len(missing)
            completed += len(rows)
            last_product_id = rows[-1][0]
            progress(
                f"Indexed {completed:,}/{expected:,}; embedded {generated:,}, copied {copied:,} this invocation",
                flush=True,
            )
        embedder.verify_identity(config["model_digest"])
        with db.transaction():
            db.execute("SET LOCAL lock_timeout='5s'")
            db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            verified = verify_build(db, build_id)
            if (
                db.execute(
                    "SELECT import_id FROM commerce.catalog_active WHERE singleton"
                ).fetchone()
                != active
            ):
                raise CatalogError(
                    "Catalog changed during embedding; rerun for the active snapshot"
                )
            if catalog_import_id:
                db.execute(
                    """INSERT INTO commerce.catalog_active(singleton,import_id) VALUES (true,%s)
                    ON CONFLICT (singleton) DO UPDATE SET import_id=EXCLUDED.import_id,activated_at=CURRENT_TIMESTAMP
                    WHERE commerce.catalog_active.import_id<>EXCLUDED.import_id""",
                    (import_id,),
                )
            db.execute(
                "UPDATE commerce.search_builds SET status='ready',completed_at=COALESCE(completed_at,CURRENT_TIMESTAMP) WHERE build_id=%s",
                (build_id,),
            )
            db.execute(
                """INSERT INTO commerce.search_active(singleton,build_id) VALUES (true,%s)
                ON CONFLICT (singleton) DO UPDATE SET build_id=EXCLUDED.build_id,activated_at=CURRENT_TIMESTAMP
                WHERE commerce.search_active.build_id<>EXCLUDED.build_id""",
                (build_id,),
            )
        reused_total = db.execute(
            "SELECT count(*) FROM commerce.search_documents WHERE build_id=%s AND embedding_reused",
            (build_id,),
        ).fetchone()[0]
        return {
            "status": "reused" if existed else "ready",
            "build_id": build_id,
            **verified,
            "embedded_this_run": generated,
            "copied_this_run": copied,
            "build_reused_vectors": reused_total,
        }
    finally:
        db.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK,))

"""Resumable export of existing vectors; immutable generations with verified publication."""

import hashlib
import math

from app.catalog import CatalogError
from app.embedding_artifacts import check_vector, resolve_vectors
from app.embedding_text import DIMENSIONS
from app.qdrant_client import CONTRACT_SHA, INDEX_CONTRACT, lexical_document, point_id
from app.search_index import verify_build
from app.storefronts import FAMILY_STOREFRONT, category_allowed


def pin_qdrant_index(db, pinned, client):
    collection = client.settings.collection(pinned["build_id"])
    row = db.execute(
        """SELECT status,contract_sha,verified_count FROM commerce.qdrant_indexes
        WHERE collection_name=%s AND build_id=%s""",
        (collection, pinned["build_id"]),
    ).fetchone()
    if row != ("ready", CONTRACT_SHA, pinned["catalog_products"]):
        raise CatalogError(
            "Qdrant index is not ready for this catalog; run scripts/build_qdrant_index.py"
        )
    validate_collection(client, collection)
    if client.count(collection) != pinned["catalog_products"]:
        raise CatalogError("Qdrant count differs from the pinned catalog; verify the index")
    return collection


def validate_collection(client, collection):
    info = client.info(collection)
    try:
        params = info["config"]["params"]
        dense = params["vectors"]["dense"]
        if (
            info["status"] not in {"green", "yellow"}
            or dense["size"] != DIMENSIONS
            or dense["distance"] != "Cosine"
            or params["sparse_vectors"]["lexical"]["modifier"] != "idf"
        ):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise CatalogError("Qdrant collection configuration is incompatible or unhealthy") from None


def source_batches(db, build_id, *, after="", batch_size=128):
    while True:
        rows = db.execute(
            """SELECT d.product_id,d.embedding_batch,d.title,d.body,d.source_line_sha256,
            d.input_sha256,p.domain,p.family FROM commerce.search_documents d
            JOIN commerce.catalog_products p ON (p.import_id,p.product_id)=(d.import_id,d.product_id)
            WHERE d.build_id=%s AND d.product_id>%s ORDER BY d.product_id LIMIT %s""",
            (build_id, after, batch_size),
        ).fetchall()
        if not rows:
            return
        vectors = resolve_vectors([(build_id, row[0], row[5], row[1]) for row in rows])
        yield [(row[0], vectors[(build_id, row[0])], *row[2:]) for row in rows]
        after = rows[-1][0]


def export_point(row, build_id, import_id):
    key, embedding, title, body, source_sha, input_sha, domain, family = row
    storefront = FAMILY_STOREFRONT.get(family)
    if not storefront or not category_allowed({"domain": domain, "family": family}, storefront):
        raise CatalogError("Product has no valid storefront assignment")
    text = title + "\n" + body
    vector = check_vector(embedding)
    return {
        "id": point_id(key),
        "vector": {"dense": vector, "lexical": lexical_document(text)},
        "payload": {
            "product_id": key,
            "build_id": build_id,
            "import_id": import_id,
            "contract_sha": CONTRACT_SHA,
            "source_sha": source_sha,
            "input_sha": input_sha,
            "lexical_sha": hashlib.sha256(text.encode()).hexdigest(),
            "domain": domain,
            "family": family,
            "storefront_id": storefront,
        },
    }


def verify_qdrant_index(db, client, build_id):
    """Check every ID, payload, and dense vector; sparse representation must exist."""
    meta = db.execute(
        "SELECT import_id,expected_count FROM commerce.search_builds WHERE build_id=%s", (build_id,)
    ).fetchone()
    if not meta:
        raise CatalogError("Unknown search build")
    collection = client.settings.collection(build_id)
    validate_collection(client, collection)
    count = 0
    for batch in source_batches(db, build_id):
        expected = [export_point(row, build_id, meta[0]) for row in batch]
        actual = client.retrieve(collection, [row["id"] for row in expected], vectors=True)
        try:
            by_id = {row["id"]: row for row in actual}
            if len(actual) != len(expected) or len(by_id) != len(actual):
                raise ValueError
            for row in expected:
                stored = by_id[row["id"]]
                dense, sparse = stored["vector"]["dense"], stored["vector"]["lexical"]
                if (
                    stored["payload"] != row["payload"]
                    or len(dense) != DIMENSIONS
                    or any(
                        not math.isfinite(a) or abs(a - b) > 1e-5
                        for a, b in zip(dense, row["vector"]["dense"], strict=True)
                    )
                    or len(sparse["indices"]) != len(sparse["values"])
                    or any(not math.isfinite(v) for v in sparse["values"])
                ):
                    raise ValueError
            count += len(expected)
        except (KeyError, ValueError, TypeError):
            raise CatalogError(
                "Qdrant contents differ from the Parquet embedding snapshot"
            ) from None
    if count != meta[1] or client.count(collection) != count:
        raise CatalogError("Qdrant index is incomplete or contains unexpected products")
    return {"collection": collection, "products": count, "build_id": build_id}


def sync_qdrant_index(db, client, build_id, progress=print):
    """Caller holds BUILD_LOCK. A ready collection is verified, never edited in place."""
    if not db.autocommit:
        raise CatalogError("Qdrant export requires an autocommit connection")
    with db.transaction():
        verify_build(db, build_id)
    meta = db.execute(
        "SELECT import_id FROM commerce.search_builds WHERE build_id=%s", (build_id,)
    ).fetchone()
    collection = client.settings.collection(build_id)
    row = db.execute(
        "SELECT status,last_product_id FROM commerce.qdrant_indexes WHERE collection_name=%s",
        (collection,),
    ).fetchone()
    if row and row[0] == "ready":
        return {"status": "reused", **verify_qdrant_index(db, client, build_id)}
    info = client.info(collection, missing_ok=True)
    if info is not None and not row:
        raise CatalogError("Unregistered Qdrant collection exists; use a new collection prefix")
    db.execute(
        """INSERT INTO commerce.qdrant_indexes(collection_name,build_id,contract_sha)
        VALUES (%s,%s,%s) ON CONFLICT (collection_name) DO NOTHING""",
        (collection, build_id, CONTRACT_SHA),
    )
    if info is None:
        client.create(collection)
        # Service storage may have been removed while an unpublished export was paused.
        if row:
            db.execute(
                "UPDATE commerce.qdrant_indexes SET last_product_id='' WHERE collection_name=%s",
                (collection,),
            )
        after = ""
    else:
        validate_collection(client, collection)
        after = row[1]
        client.ensure_payload_indexes(collection)
    for batch in source_batches(db, build_id, after=after):
        client.upsert(collection, [export_point(row, build_id, meta[0]) for row in batch])
        # Advance only after wait=true acknowledged every point; retrying IDs is idempotent.
        db.execute(
            "UPDATE commerce.qdrant_indexes SET last_product_id=%s WHERE collection_name=%s",
            (batch[-1][0], collection),
        )
        progress(f"Qdrant exported through {batch[-1][0]}", flush=True)
    verified = verify_qdrant_index(db, client, build_id)
    db.execute(
        """UPDATE commerce.qdrant_indexes SET status='ready',verified_count=%s,
        verified_at=CURRENT_TIMESTAMP WHERE collection_name=%s""",
        (verified["products"], collection),
    )
    return {"status": "ready", **verified, "embedding_calls": 0, "contract": INDEX_CONTRACT}

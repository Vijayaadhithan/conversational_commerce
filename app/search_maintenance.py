"""Explicit removal and index maintenance within the dedicated commerce DB."""

import re
from contextlib import contextmanager

from app.catalog import LOCK_ID, CatalogError, _check_schema
from app.search_index import BUILD_LOCK


@contextmanager
def maintenance_lock(db):
    if not db.autocommit:
        raise CatalogError("Search maintenance requires an autocommit connection")
    if not db.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK,)).fetchone()[0]:
        raise CatalogError("Another embedding build or maintenance operation is running")
    catalog_locked = False
    try:
        catalog_locked = db.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
        if not catalog_locked:
            raise CatalogError("Catalog import or migration is running; retry maintenance later")
        _check_schema(db)
        yield
    finally:
        if catalog_locked:
            db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        db.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK,))


def remove_builds(
    db, *, build_ids=None, keep_ready=1, include_incomplete=False, apply=False, qdrant=None
):
    """Default to a concrete dry run; active build is protected even on explicit delete."""
    if type(keep_ready) is not int or keep_ready < 0:
        raise CatalogError("keep-ready must be a non-negative integer")
    if build_ids is not None and (
        not build_ids or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in build_ids)
    ):
        raise CatalogError("Specify valid build IDs for deletion")
    with maintenance_lock(db), db.transaction():
        rows = db.execute("""SELECT b.build_id,b.status,count(d.product_id),a.build_id IS NOT NULL
            FROM commerce.search_builds b LEFT JOIN commerce.search_documents d USING(build_id)
            LEFT JOIN commerce.search_active a ON a.build_id=b.build_id
            GROUP BY b.build_id,a.build_id
            ORDER BY b.completed_at DESC NULLS LAST,b.created_at DESC,b.build_id""").fetchall()
        requested = set(build_ids or [])
        if requested - {row[0] for row in rows}:
            raise CatalogError("A requested build does not exist")
        if any(row[3] and row[0] in requested for row in rows):
            raise CatalogError("Cannot delete the active search build")
        delete, preserved = [], []
        kept = 0
        for build_id, status, count, active in rows:
            item = {"build_id": build_id, "status": status, "vectors": count}
            if active:
                reason = "active"
            elif build_ids is not None:
                reason = None if build_id in requested else "not_selected"
            elif status != "ready":
                reason = None if include_incomplete else "resumable_incomplete"
            elif kept < keep_ready:
                kept += 1
                reason = "retained_rollback"
            else:
                reason = None
            if reason:
                preserved.append({**item, "reason": reason})
            else:
                delete.append(item)
        ids = [item["build_id"] for item in delete]
        collections = db.execute(
            "SELECT collection_name,build_id FROM commerce.qdrant_indexes "
            "WHERE build_id=ANY(%s) ORDER BY collection_name",
            (ids,),
        ).fetchall()
        if apply and collections:
            if qdrant is None:
                raise CatalogError("Qdrant generations require explicit remote cleanup")
            aliases = qdrant.request("GET", "/aliases")["aliases"]
            aliased = {item["collection_name"] for item in aliases}
            for name, build_id in collections:
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name) or not name.endswith(
                    "_" + build_id
                ):
                    raise CatalogError("Unexpected Qdrant collection identity; inspect manually")
                if name in aliased:
                    raise CatalogError("Cannot remove an aliased Qdrant collection")
            # Remove remote data before cascading registry rows. On failure the SQL
            # transaction rolls back; retry accepts already deleted collections.
            for name, _ in collections:
                qdrant.request("DELETE", f"/collections/{name}", missing_ok=True)
        if apply and ids:
            db.execute("DELETE FROM commerce.search_documents WHERE build_id=ANY(%s)", (ids,))
            db.execute("DELETE FROM commerce.search_builds WHERE build_id=ANY(%s)", (ids,))
        return {
            "status": "deleted" if apply else "planned",
            "apply": apply,
            "builds": delete,
            "vector_rows": sum(item["vectors"] for item in delete),
            "preserved": preserved,
            "qdrant_collections": [name for name, _ in collections],
        }


def remove_imports(db, *, import_ids=None, min_age_days=7, apply=False):
    """Prune only unreferenced imports; explicit IDs override age, never references."""
    if type(min_age_days) is not int or min_age_days < 0:
        raise CatalogError("Minimum age must be a non-negative integer")
    if import_ids is not None and (
        not import_ids or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in import_ids)
    ):
        raise CatalogError("Specify valid import IDs for deletion")
    with maintenance_lock(db), db.transaction():
        rows = db.execute(
            """SELECT i.import_id,i.product_count,
                EXISTS(SELECT 1 FROM commerce.catalog_active a WHERE a.import_id=i.import_id),
                EXISTS(SELECT 1 FROM commerce.search_builds b WHERE b.import_id=i.import_id),
                i.imported_at <= CURRENT_TIMESTAMP - (%s * interval '1 day')
                FROM commerce.catalog_imports i ORDER BY i.imported_at DESC,i.import_id""",
            (min_age_days,),
        ).fetchall()
        requested = set(import_ids or [])
        if requested - {row[0] for row in rows}:
            raise CatalogError("A requested import does not exist")
        if any((row[2] or row[3]) and row[0] in requested for row in rows):
            raise CatalogError("Cannot delete an active or search-referenced catalog import")
        delete, preserved = [], []
        for import_id, count, active, referenced, old in rows:
            item = {"import_id": import_id, "products": count}
            if active:
                reason = "active"
            elif referenced:
                reason = "search_referenced"
            elif import_ids is not None:
                reason = None if import_id in requested else "not_selected"
            else:
                reason = None if old else "retained_recent"
            if reason:
                preserved.append({**item, "reason": reason})
            else:
                delete.append(item)
        ids = [item["import_id"] for item in delete]
        if apply and ids:
            db.execute("DELETE FROM commerce.catalog_products WHERE import_id=ANY(%s)", (ids,))
            db.execute("DELETE FROM commerce.catalog_imports WHERE import_id=ANY(%s)", (ids,))
        return {
            "status": "deleted" if apply else "planned",
            "apply": apply,
            "imports": delete,
            "product_rows": sum(item["products"] for item in delete),
            "preserved": preserved,
        }


def maintain_indexes(db, operation):
    with maintenance_lock(db):
        if operation == "reindex":
            db.execute("REINDEX TABLE CONCURRENTLY commerce.search_documents")
            db.execute("REINDEX TABLE CONCURRENTLY commerce.catalog_products")
            db.execute("ANALYZE commerce.search_documents")
            db.execute("ANALYZE commerce.catalog_products")
        elif operation == "vacuum":
            db.execute("VACUUM (ANALYZE) commerce.search_documents")
            db.execute("VACUUM (ANALYZE) commerce.search_builds")
            db.execute("VACUUM (ANALYZE) commerce.catalog_products")
            db.execute("VACUUM (ANALYZE) commerce.catalog_imports")
        else:
            raise CatalogError("Unsupported index maintenance operation")
        indexes = db.execute("""SELECT c.relname,i.indisvalid,i.indisready FROM pg_index i
            JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname='commerce' AND t.relname IN ('search_documents','catalog_products')
            ORDER BY c.relname""").fetchall()
        if not indexes or any(not valid or not ready for name, valid, ready in indexes):
            raise CatalogError(
                "An index is invalid; inspect PostgreSQL before retrying maintenance"
            )
        return {
            "status": "complete",
            "operation": operation,
            "indexes": [
                {"name": name, "valid": valid, "ready": ready} for name, valid, ready in indexes
            ],
        }

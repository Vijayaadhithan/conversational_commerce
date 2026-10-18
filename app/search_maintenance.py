"""Explicit removal and index maintenance within the dedicated commerce DB."""

from contextlib import contextmanager
import re

from app.catalog import CatalogError, LOCK_ID, _check_schema
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
        if catalog_locked: db.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        db.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK,))


def remove_builds(db, *, build_ids=None, keep_ready=1, include_incomplete=False, apply=False):
    """Default to a concrete dry run; active build is protected even on explicit delete."""
    if type(keep_ready) is not int or keep_ready < 0:
        raise CatalogError("keep-ready must be a non-negative integer")
    if build_ids is not None and (not build_ids or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in build_ids)):
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
            if reason: preserved.append({**item, "reason": reason})
            else: delete.append(item)
        ids = [item["build_id"] for item in delete]
        if apply and ids:
            db.execute("DELETE FROM commerce.search_documents WHERE build_id=ANY(%s)", (ids,))
            db.execute("DELETE FROM commerce.search_builds WHERE build_id=ANY(%s)", (ids,))
        return {"status": "deleted" if apply else "planned", "apply": apply,
                "builds": delete, "vector_rows": sum(item["vectors"] for item in delete),
                "preserved": preserved}


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
        else:
            raise CatalogError("Unsupported index maintenance operation")
        indexes = db.execute("""SELECT c.relname,i.indisvalid,i.indisready FROM pg_index i
            JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname='commerce' AND t.relname IN ('search_documents','catalog_products')
            ORDER BY c.relname""").fetchall()
        if not indexes or any(not valid or not ready for name, valid, ready in indexes):
            raise CatalogError("An index is invalid; inspect PostgreSQL before retrying maintenance")
        return {"status": "complete", "operation": operation,
                "indexes": [{"name": name, "valid": valid, "ready": ready} for name, valid, ready in indexes]}

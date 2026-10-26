"""Incremental add/update/remove, refresh, retention, and physical index maintenance."""

from copy import deepcopy

import pytest
from test_catalog import make_source, product
from test_search import FakeEmbedder

from app import search_index
from app.catalog import CatalogError, connect_database, import_catalog, validated_source
from app.product_search import ProductSearchRequest, pin_search_build, search_catalog
from app.qdrant_client import QdrantClient, QdrantSettings
from app.search_index import BUILD_LOCK, build_index, verify_build
from app.search_maintenance import maintain_indexes, remove_builds, remove_imports


def retire(db, **kwargs):
    with QdrantClient(QdrantSettings.from_env()) as client:
        return remove_builds(db, qdrant=client, **kwargs)


def QUIET(*args, **kwargs):
    pass


def records(count=3):
    rows = []
    for number in range(1, count + 1):
        row = product(f"B{number:09d}")
        row["title"] = f"Distinct listing {number} moisturizer"
        rows.append(row)
    return rows


def publish(pg, root, rows):
    make_source(root, rows)
    with validated_source(root) as source:
        result = import_catalog(pg, source)
    pg.commit()
    return result


def test_incremental_add_change_price_only_lexical_only_and_delete(pg, scratch_database, tmp_path):
    original = records(5)
    original[4]["content"]["description"] = ["listing words " * 200 + "oldkeyword"]
    publish(pg, tmp_path, original)
    with connect_database(scratch_database, autocommit=True) as db:
        first = build_index(db, FakeEmbedder(), QUIET)
        assert first["embedded_this_run"] == 5
        updated = deepcopy(original)
        updated[1]["title"] = "Different moisturizer formula"
        updated[2]["price"].update(amount="9.25", observed_value="9.25")
        updated[4]["content"]["description"] = ["listing words " * 200 + "newkeyword"]
        updated.pop(3)  # Parent product removed from the authoritative snapshot.
        added = product("B000000006")
        added["title"] = "A newly added moisturizer"
        updated.append(added)
        publish(pg, tmp_path, updated)
        model = FakeEmbedder(batch_size=2)
        second = build_index(db, model, QUIET)
        assert second["embedded_this_run"] == 2
        assert second["copied_this_run"] == 3 and second["products"] == 5
        assert db.execute(
            "SELECT count(*) FROM commerce.search_documents WHERE build_id=%s AND product_id='amazon:B000000004'",
            (second["build_id"],),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT historical_price::text FROM commerce.active_products WHERE product_id='amazon:B000000003'"
        ).fetchone() == ("9.25",)
        body = db.execute(
            "SELECT body FROM commerce.search_documents WHERE build_id=%s AND product_id='amazon:B000000005'",
            (second["build_id"],),
        ).fetchone()[0]
        assert "newkeyword" in body and "oldkeyword" not in body
        with db.transaction():
            assert verify_build(db, second["build_id"])["products"] == 5
        assert build_index(db, FakeEmbedder(fail_after=0), QUIET)["embedded_this_run"] == 0


def test_model_or_input_contract_change_invalidates_cached_vectors(
    pg, scratch_database, tmp_path, monkeypatch
):
    publish(pg, tmp_path, records(2))
    with connect_database(scratch_database, autocommit=True) as db:
        first = build_index(db, FakeEmbedder(), QUIET)
        different_model = FakeEmbedder()
        different_model.digest = "b" * 64
        second = build_index(db, different_model, QUIET)
        assert second["build_id"] != first["build_id"] and second["embedded_this_run"] == 2
        changed_contract = {**search_index.contract(), "version": "test-new-input-contract"}
        monkeypatch.setattr(search_index, "contract", lambda: changed_contract)
        third = build_index(db, different_model, QUIET)
        assert third["embedded_this_run"] == 2 and third["copied_this_run"] == 0


def test_selective_full_refresh_and_default_preserves_refreshed_generation(
    pg, scratch_database, tmp_path
):
    publish(pg, tmp_path, records())
    with connect_database(scratch_database, autocommit=True) as db:
        first = build_index(db, FakeEmbedder(), QUIET)
        selected = build_index(db, FakeEmbedder(), QUIET, reembed_ids=["amazon:B000000002"])
        assert selected["build_id"] != first["build_id"]
        assert (selected["embedded_this_run"], selected["copied_this_run"]) == (1, 2)
        full = build_index(db, FakeEmbedder(), QUIET, reembed_all=True)
        assert (full["embedded_this_run"], full["copied_this_run"]) == (3, 0)
        same = build_index(db, FakeEmbedder(fail_after=0), QUIET)
        assert same["build_id"] == full["build_id"] and same["status"] == "reused"
        with pytest.raises(CatalogError, match="missing from the active catalog"):
            build_index(db, FakeEmbedder(), QUIET, reembed_ids=["amazon:B999999999"])


def test_failed_full_refresh_resumes_without_replacing_active_build(pg, scratch_database, tmp_path):
    publish(pg, tmp_path, records())
    with connect_database(scratch_database, autocommit=True) as db:
        old = build_index(db, FakeEmbedder(), QUIET)
        with pytest.raises(CatalogError, match="Injected"):
            build_index(db, FakeEmbedder(fail_after=1), QUIET, reembed_all=True)
        incomplete = db.execute(
            "SELECT build_id FROM commerce.search_builds WHERE status='building'"
        ).fetchone()[0]
        assert db.execute("SELECT build_id FROM commerce.search_active").fetchone() == (
            old["build_id"],
        )
        assert db.execute(
            "SELECT count(*) FROM commerce.search_documents WHERE build_id=%s", (incomplete,)
        ).fetchone() == (1,)
        resumed = build_index(db, FakeEmbedder(), QUIET, resume_build=incomplete)
        assert resumed["build_id"] == incomplete and resumed["embedded_this_run"] == 2
        assert (
            build_index(db, FakeEmbedder(fail_after=0), QUIET, resume_build=incomplete)["status"]
            == "reused"
        )
        with pytest.raises(CatalogError, match="Resume cannot be combined"):
            build_index(db, FakeEmbedder(), QUIET, resume_build=incomplete, reembed_all=True)


def test_empty_snapshot_removes_every_product_and_old_vectors_can_be_pruned(
    pg, scratch_database, tmp_path
):
    publish(pg, tmp_path, records())
    with connect_database(scratch_database, autocommit=True) as db:
        build_index(db, FakeEmbedder(), QUIET)
        publish(pg, tmp_path, [])
        empty = build_index(db, FakeEmbedder(fail_after=0), QUIET)
        assert empty["products"] == 0 and empty["embedded_this_run"] == 0
        result = search_catalog(
            pg, ProductSearchRequest(query="moisturizer"), FakeEmbedder(fail_after=0)
        )
        assert result["results"] == [] and result["eligible_products"] == 0
        pg.commit()
        preview = remove_builds(db, keep_ready=0)
        assert preview["vector_rows"] == 3
        assert db.execute("SELECT count(*) FROM commerce.search_documents").fetchone() == (3,)
        removed = retire(db, keep_ready=0, apply=True)
        assert removed["vector_rows"] == 3
        assert db.execute("SELECT count(*) FROM commerce.search_documents").fetchone() == (0,)
        assert db.execute("SELECT build_id FROM commerce.search_active").fetchone() == (
            empty["build_id"],
        )
        # Deleting vectors does not erase source/catalog provenance.
        assert db.execute("SELECT count(*) FROM commerce.catalog_products").fetchone() == (3,)
        with pytest.raises(CatalogError, match="active search build"):
            remove_builds(db, build_ids=[empty["build_id"]], apply=True)


def test_retention_delete_protection_and_incomplete_opt_in(pg, scratch_database, tmp_path):
    publish(pg, tmp_path, records(2))
    with connect_database(scratch_database, autocommit=True) as db:
        old = build_index(db, FakeEmbedder(), QUIET)
        rollback = build_index(db, FakeEmbedder(), QUIET, reembed_all=True)
        active = build_index(db, FakeEmbedder(), QUIET, reembed_all=True)
        with pytest.raises(CatalogError):
            build_index(db, FakeEmbedder(fail_after=0), QUIET, reembed_all=True)
        preview = remove_builds(db)
        assert [item["build_id"] for item in preview["builds"]] == [old["build_id"]]
        assert {item["reason"] for item in preview["preserved"]} == {
            "active",
            "retained_rollback",
            "resumable_incomplete",
        }
        with pytest.raises(CatalogError):
            remove_builds(db, build_ids=[rollback["build_id"], active["build_id"]], apply=True)
        assert db.execute("SELECT count(*) FROM commerce.search_builds").fetchone() == (4,)
        retire(db, build_ids=[rollback["build_id"]], apply=True)
        assert db.execute(
            "SELECT count(*) FROM commerce.search_documents WHERE build_id=%s",
            (rollback["build_id"],),
        ).fetchone() == (0,)
        result = retire(db, keep_ready=0, include_incomplete=True, apply=True)
        assert len(result["builds"]) == 2
        assert db.execute("SELECT build_id FROM commerce.search_builds").fetchall() == [
            (active["build_id"],)
        ]


def test_reindex_vacuum_and_busy_builder_protection(pg, scratch_database, tmp_path):
    publish(pg, tmp_path, records())
    with connect_database(scratch_database, autocommit=True) as db:
        build = build_index(db, FakeEmbedder(), QUIET)
        with connect_database(scratch_database, autocommit=True) as busy:
            busy.execute("SELECT pg_advisory_lock(%s)", (BUILD_LOCK,))
            with pytest.raises(CatalogError, match="running"):
                retire(db, keep_ready=0, apply=True)
        rebuilt = maintain_indexes(db, "reindex")
        assert rebuilt["indexes"] and all(item["valid"] for item in rebuilt["indexes"])
        assert maintain_indexes(db, "vacuum")["status"] == "complete"
        with db.transaction():
            assert verify_build(db, build["build_id"])["products"] == 3


def test_reader_snapshot_survives_publication_before_offline_pruning(
    pg, scratch_database, tmp_path
):
    publish(pg, tmp_path, records())
    with connect_database(scratch_database, autocommit=True) as db:
        old = build_index(db, FakeEmbedder(), QUIET)
        pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        pinned = pin_search_build(pg)
        build_index(db, FakeEmbedder(), QUIET, reembed_all=True)
        response = search_catalog(
            pg, ProductSearchRequest(query="moisturizer"), FakeEmbedder(), pinned
        )
        assert len(response["results"]) == 3 and response["build_id"] == old["build_id"]
        pg.rollback()  # Remote retirement requires readers to finish first.
        retire(db, build_ids=[old["build_id"]], apply=True)


def test_catalog_cleanup_protects_active_rollback_and_staged_imports(
    pg, scratch_database, tmp_path
):
    publish(pg, tmp_path, records(1))
    with connect_database(scratch_database, autocommit=True) as db:
        old = build_index(db, FakeEmbedder(), QUIET)
        old_import = db.execute(
            "SELECT import_id FROM commerce.search_builds WHERE build_id=%s", (old["build_id"],)
        ).fetchone()[0]
        publish(pg, tmp_path, records(2))
        build_index(db, FakeEmbedder(), QUIET)
        assert remove_imports(db)["imports"] == []
        with pytest.raises(CatalogError, match="search-referenced"):
            remove_imports(db, import_ids=[old_import], apply=True)
        retire(db, build_ids=[old["build_id"]], apply=True)
        assert remove_imports(db)["imports"] == []  # Recent orphan may be staged for a build.
        plan = remove_imports(db, import_ids=[old_import])
        assert plan["product_rows"] == 1
        assert db.execute("SELECT count(*) FROM commerce.catalog_imports").fetchone() == (2,)
        active = db.execute("SELECT import_id FROM commerce.catalog_active").fetchone()[0]
        with pytest.raises(CatalogError, match="active"):
            remove_imports(db, import_ids=[old_import, active], apply=True)
        result = remove_imports(db, import_ids=[old_import], apply=True)
        assert result["product_rows"] == 1
        assert db.execute("SELECT count(*) FROM commerce.catalog_products").fetchone() == (2,)


def test_qdrant_cleanup_requires_opt_in_checks_aliases_and_recovers_from_failure(
    pg, scratch_database, tmp_path
):
    publish(pg, tmp_path, records(1))
    with connect_database(scratch_database, autocommit=True) as db:
        old = build_index(db, FakeEmbedder(), QUIET)
        build_index(db, FakeEmbedder(), QUIET, reembed_all=True)
        db.execute("DELETE FROM commerce.qdrant_indexes WHERE build_id=%s", (old["build_id"],))
        name = "commerce_test_" + old["build_id"]
        db.execute(
            "INSERT INTO commerce.qdrant_indexes(collection_name,build_id,contract_sha) "
            "VALUES (%s,%s,%s)",
            (name, old["build_id"], "a" * 64),
        )
        assert remove_builds(db, keep_ready=0)["qdrant_collections"] == [name]
        with pytest.raises(CatalogError, match="explicit remote"):
            remove_builds(db, keep_ready=0, apply=True)

        class Remote:
            aliased = True
            fail = False
            deleted = []

            def request(self, method, path, *, missing_ok=False):
                if method == "GET":
                    return {"aliases": [{"collection_name": name}] if self.aliased else []}
                assert missing_ok and path == f"/collections/{name}"
                if self.fail:
                    raise CatalogError("Injected remote failure")
                self.deleted.append(name)
                return None  # Collection already absent after an interrupted attempt.

        remote = Remote()
        with pytest.raises(CatalogError, match="aliased"):
            remove_builds(db, keep_ready=0, apply=True, qdrant=remote)
        assert not remote.deleted
        remote.aliased = False
        remote.fail = True
        with pytest.raises(CatalogError, match="Injected"):
            remove_builds(db, keep_ready=0, apply=True, qdrant=remote)
        assert db.execute("SELECT count(*) FROM commerce.qdrant_indexes").fetchone() == (2,)
        remote.fail = False
        remove_builds(db, keep_ready=0, apply=True, qdrant=remote)
        assert remote.deleted == [name]
        assert db.execute("SELECT count(*) FROM commerce.qdrant_indexes").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM commerce.search_builds").fetchone() == (1,)

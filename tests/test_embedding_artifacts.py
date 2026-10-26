"""Durable embedding snapshots, integrity checks, and protected artifact retention."""

import pytest
from test_embedding_lifecycle import QUIET, publish, records, retire
from test_search import FakeEmbedder

from app.catalog import CatalogError, connect_database
from app.embedding_artifacts import build_directory, read_batch, resolve_vectors, write_batch
from app.search_index import build_index, verify_build
from app.search_maintenance import prune_artifacts

BUILD = "a" * 64
INPUT = "b" * 64
KEY = "amazon:B000000001"
VECTOR = [1.0] + [0.0] * 767


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_ARTIFACT_DIR", str(tmp_path / "embeddings"))


def test_batch_roundtrip_and_identity(artifacts):
    batch = write_batch(BUILD, [(KEY, INPUT, VECTOR)])
    assert read_batch(BUILD, batch) == {KEY: (INPUT, VECTOR)}
    assert resolve_vectors([(BUILD, KEY, INPUT, batch)]) == {(BUILD, KEY): VECTOR}
    assert list(build_directory(BUILD).iterdir()) == [build_directory(BUILD) / (batch + ".parquet")]
    with pytest.raises(CatalogError, match="differs"):
        resolve_vectors([(BUILD, KEY, "c" * 64, batch)])
    with pytest.raises(CatalogError, match="missing or unreadable"):
        read_batch("d" * 64, batch)


@pytest.mark.parametrize("vector", [[], [0.0] * 768, [float("nan")] * 768, [True] * 768])
def test_invalid_vectors_never_publish(artifacts, vector):
    with pytest.raises(CatalogError, match="normalized vector"):
        write_batch(BUILD, [(KEY, INPUT, vector)])
    assert not list(build_directory(BUILD).glob("*.parquet"))


def test_corruption_and_symlinks_are_rejected(artifacts, tmp_path):
    batch = write_batch(BUILD, [(KEY, INPUT, VECTOR)])
    path = build_directory(BUILD) / (batch + ".parquet")
    path.write_bytes(b"corrupt")
    with pytest.raises(CatalogError, match="checksum"):
        read_batch(BUILD, batch)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(CatalogError, match="checksum"):
        read_batch(BUILD, batch)
    with pytest.raises(CatalogError, match="identity"):
        build_directory("../outside")


def test_metadata_only_schema_and_corrupt_snapshot_blocks_reuse(pg, scratch_database, tmp_path):
    assert pg.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone() is None
    columns = {
        row[0]
        for row in pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='commerce' AND table_name='search_documents'"
        )
    }
    assert "embedding_batch" in columns and "embedding" not in columns
    publish(pg, tmp_path, records(1))
    with connect_database(scratch_database, autocommit=True) as db:
        built = build_index(db, FakeEmbedder(), QUIET)
        batch = db.execute("SELECT embedding_batch FROM commerce.search_documents").fetchone()[0]
        (build_directory(built["build_id"]) / (batch + ".parquet")).unlink()
        with db.transaction(), pytest.raises(CatalogError, match="missing or unreadable"):
            verify_build(db, built["build_id"])
        with pytest.raises(CatalogError, match="missing or unreadable"):
            build_index(db, FakeEmbedder(fail_after=0), QUIET)


def test_artifact_retention_protects_active_and_incomplete(pg, scratch_database, tmp_path):
    publish(pg, tmp_path, records(2))
    with connect_database(scratch_database, autocommit=True) as db:
        first = build_index(db, FakeEmbedder(), QUIET)
        second = build_index(db, FakeEmbedder(), QUIET, reembed_all=True)
        db.execute(
            "UPDATE commerce.search_builds SET status='building' WHERE build_id=%s",
            (first["build_id"],),
        )
        assert prune_artifacts(db, apply=True)["directories"] == []
        retire(db, build_ids=[first["build_id"]], apply=True)
        path = build_directory(first["build_id"])
        assert prune_artifacts(db)["directories"] == [str(path)]
        assert path.exists()
        assert prune_artifacts(db, apply=True)["directories"] == [str(path)]
        assert not path.exists()
        assert prune_artifacts(db, apply=True)["directories"] == []
        with db.transaction():
            assert verify_build(db, second["build_id"])["products"] == 2

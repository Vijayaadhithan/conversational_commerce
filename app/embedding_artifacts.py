"""Immutable, checksummed Parquet embedding batches; PostgreSQL stores references only."""

import hashlib
import math
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

import duckdb

from app.catalog import ROOT, CatalogError
from app.embedding_text import DIMENSIONS

STORAGE_VERSION = "parquet-v1"


def artifact_root():
    path = Path(os.getenv("SEARCH_ARTIFACT_DIR", "data/search/embeddings")).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def build_directory(build_id):
    if not re.fullmatch(r"[0-9a-f]{64}", build_id):
        raise CatalogError("Invalid embedding build identity")
    path = artifact_root() / build_id
    if path.is_symlink():
        raise CatalogError("Embedding directories cannot be symlinks")
    return path


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_vector(vector):
    if (
        not isinstance(vector, (list, tuple))
        or len(vector) != DIMENSIONS
        or any(type(value) not in (float, int) or not math.isfinite(value) for value in vector)
        or not 0.999 <= sum(value * value for value in vector) ** 0.5 <= 1.001
    ):
        raise CatalogError("Embedding artifact contains an invalid normalized vector")
    return list(vector)


def write_batch(build_id, rows):
    """Persist (product_id, input_sha256, vector) before committing SQL references."""
    if not rows:
        raise CatalogError("Cannot write an empty embedding batch")
    directory = build_directory(build_id)
    directory.mkdir(parents=True, exist_ok=True)
    checked = []
    for key, sha, vector in rows:
        if not re.fullmatch(r"amazon:[A-Z0-9]{10}", key) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise CatalogError("Invalid embedding row identity")
        checked.append((key, sha, check_vector(vector)))
    with NamedTemporaryFile(dir=directory, suffix=".building", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as db:
            db.execute(
                f"CREATE TABLE embeddings(product_id VARCHAR PRIMARY KEY, input_sha256 VARCHAR, embedding FLOAT[{DIMENSIONS}])"
            )
            db.executemany("INSERT INTO embeddings VALUES (?,?,?)", checked)
            db.execute(
                "COPY (SELECT * FROM embeddings ORDER BY product_id) TO ? "
                "(FORMAT PARQUET, COMPRESSION ZSTD)",
                [str(temporary)],
            )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        batch = checksum(temporary)
        temporary.replace(directory / (batch + ".parquet"))
        # Persist the directory entry before a SQL commit can reference it.
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return batch
    finally:
        temporary.unlink(missing_ok=True)


def read_batch(build_id, batch):
    if not isinstance(batch, str) or not re.fullmatch(r"[0-9a-f]{64}", batch):
        raise CatalogError("Invalid embedding batch identity")
    path = build_directory(build_id) / (batch + ".parquet")
    try:
        if path.is_symlink() or checksum(path) != batch:
            raise CatalogError("Embedding artifact checksum mismatch")
        with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as db:
            rows = db.execute(
                "SELECT product_id,input_sha256,embedding FROM read_parquet(?) ORDER BY product_id",
                [str(path)],
            ).fetchall()
        result = {}
        for key, sha, vector in rows:
            if key in result:
                raise CatalogError("Duplicate product in embedding artifact")
            result[key] = (sha, check_vector(vector))
        return result
    except (OSError, duckdb.Error) as exc:
        raise CatalogError(
            "Embedding artifact is missing or unreadable; restore the snapshot"
        ) from exc


def resolve_vectors(references):
    """Resolve a bounded set of (build_id, product_id, input_sha256, batch) references."""
    batches = {}
    result = {}
    for build_id, key, sha, batch in references:
        identity = (build_id, batch)
        if identity not in batches:
            batches[identity] = read_batch(build_id, batch)
        item = batches[identity].get(key)
        if item is None or item[0] != sha:
            raise CatalogError("Embedding artifact differs from its catalog metadata")
        result[(build_id, key)] = item[1]
    return result

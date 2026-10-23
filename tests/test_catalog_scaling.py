import gzip
import json
from pathlib import Path

import pytest
from test_catalog import make_source, product

from app.catalog import CatalogError, validated_source
from app.catalog_sampling import expand_sample
from app.enrichment_pipeline import fingerprint


def full_catalog(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    rows = [product(f"B{i:09d}") for i in range(1, 9)]
    # Preserve an existing member whose ID is late in the archive.
    _, run, manifest = make_source(source, [rows[-1]])
    (run / "catalog.jsonl.gz").write_bytes(
        gzip.compress("".join(json.dumps(row) + "\n" for row in rows).encode())
    )
    manifest["outputs"]["catalog.jsonl.gz"] = fingerprint(run / "catalog.jsonl.gz")
    manifest["counts"]["unique_products"] = len(rows)
    (run / "manifest.json").write_text(json.dumps(manifest))
    return source


def test_expansion_retains_existing_ids_and_is_reproducible(tmp_path):
    source = full_catalog(tmp_path)
    result = expand_sample(source, tmp_path / "larger", 5)
    assert result["products"] == 5 and result["retained"] == 1
    with validated_source(tmp_path / "larger") as selected:
        ids = [row["product_id"] for _, _, row in selected.rows()]
    assert "amazon:B000000008" in ids and len(set(ids)) == 5
    assert expand_sample(source, tmp_path / "larger", 5) == result
    second = expand_sample(source, tmp_path / "second", 5)
    assert (
        Path(result["manifest"]).with_name("subset.jsonl").read_bytes()
        == Path(second["manifest"]).with_name("subset.jsonl").read_bytes()
    )


def test_requested_expansion_cannot_silently_shortfall(tmp_path):
    source = full_catalog(tmp_path)
    with pytest.raises(CatalogError, match="exceeds"):
        expand_sample(source, tmp_path / "too_large", 20)
    assert not (tmp_path / "too_large/current.json").exists()

"""Evidence boundaries, immutable snapshot publication and concern isolation."""
from copy import deepcopy
import gzip
import hashlib
import json

import pytest

from app.catalog import CatalogError, validated_source
from app.catalog_enrichment import enrich_record
from app.commerce import explain_products
from app.commerce_models import Concern, ShoppingTask
from app.enrichment_pipeline import publish_enrichment
from app.listing_explanations import concern_evidence
from test_catalog import make_source, product


def listing(*features):
    row = product()
    row["content"]["features"] = list(features)
    row["content_source_fields"]["features"] = [f"/features/{i}" for i in range(len(features))]
    return row


def test_metadata_keeps_provenance_and_does_not_invent_ingredients_or_variants():
    original = listing("Ingredients: Water, Glycerin;", "Available sizes: US 6, US 7.", "For dry skin.")
    raw = {"details": {"Skin type": "Sensitive", "Product Benefits": "Hydrating", "Item Volume": "100 ml", "Style": "Classic", "Special Ingredients": "Aloe"}}
    row = enrich_record(original, raw)
    assert original["attributes"] == {"skin_type": None, "fragrance_free": None}
    assert row["attributes"]["skin_type"] == {"value": "Sensitive", "field": "/details/Skin type", "status": "listing_claim"}
    assert row["attributes"]["ingredients"]["value"] == "Water, Glycerin"
    assert row["attributes"]["ingredients"]["field"] == "/features/0"
    assert row["attributes"]["featured_ingredients"]["value"] == "Aloe"
    assert row["attributes"]["volume"]["value"] == "100 ml"
    assert row["attributes"]["benefits"]["value"] == "Hydrating"
    assert row["attributes"]["size_options"]["value"] == "US 6, US 7"
    assert not row["attributes"].get("size")
    assert row["availability"] is None
    for key in ("selected_observation", "observations", "price", "category", "content"):
        assert row[key] == original[key]


@pytest.mark.parametrize("text", ["Not for sensitive skin.", "Avoid using for sensitive skin.",
    "For dry skin and oily skin.", "For dry skin or sensitive skin.", "With glycerin and no added fragrance."])
def test_negative_ambiguous_and_unlabeled_metadata_remain_unknown(text):
    attrs = enrich_record(listing(text), {"details": {"Package Dimensions": "12 x 8 x 2 inches"}})["attributes"]
    for name in ("ingredients", "skin_type", "size", "volume", "fragrance_free"):
        assert not attrs.get(name)


def test_conflicting_values_and_product_bundles_not_collapsed():
    row = listing("Skin Type: Dry.", "Skin Type: Oily.")
    row["title"] = "Cleanser and moisturizer kit"
    attrs = enrich_record(row, {"details": {"Item Volume": "100 ml", "Liquid Volume": "200 ml"}})["attributes"]
    assert not attrs.get("skin_type")
    assert not attrs.get("volume")
    assert not attrs.get("product_type")


def test_decimal_sizes_and_abbreviated_ingredients_are_not_truncated():
    row = enrich_record(listing("Size: 4.5 inches.", "Ingredients: Alcohol Denat., Water, Glycerin.",
                               "Available sizes: US 7.5, US 8."), {})
    assert row["attributes"]["size"]["value"] == "4.5 inches"
    assert row["attributes"]["ingredients"]["value"] == "Alcohol Denat., Water, Glycerin"
    assert row["attributes"]["size_options"]["value"] == "US 7.5, US 8"
    attrs = enrich_record(listing("Ingredients: We focus on healthy ingredients.", "Size: 200 ml.",
                                  "Size: 8--30 inch in stock."), {})["attributes"]
    assert not attrs.get("ingredients") and not attrs.get("size")


def test_structured_skin_type_is_evidence_without_fabricated_quote():
    row = enrich_record(product(), {"details": {"Skin Type": "Dry"}})
    evidence = concern_evidence(row, ["I have dry skin"])[0]
    assert evidence["status"] == "listing_mention"
    assert evidence["evidence"][0]["text"] == "Dry"
    assert evidence["evidence"][0]["field"] == "/details/Skin Type"


def test_concerns_use_full_listing_separately_and_preserve_negation_and_user_spelling():
    row = listing("Unrelated marketing text.", "Not suitable for sensitive skin.", "Brightening facial lotion.", "Acne blemish care.")
    concerns = ["lightens my face", "reduce pimble", "sensitive skin", "kess chemincal", "cotton lining"]
    items = concern_evidence(row, concerns)
    assert [i["concern"] for i in items] == concerns
    assert [i["status"] for i in items] == ["listing_mention"] * 3 + ["needs_clarification", "not_stated"]
    assert items[0]["evidence"][0]["field"] == "/features/2"
    assert items[1]["evidence"][0]["field"] == "/features/3"
    assert items[2]["evidence"][0]["text"].startswith("Not suitable")
    assert not items[3]["evidence"] and not items[4]["evidence"]
    task = ShoppingTask(id=1, needs=[Concern(value=c, evidence=c) for c in concerns])
    row["concern_evidence"] = items
    result = explain_products([row], task)[0]
    assert len(result["why_recommended"]) == 5
    assert all(c in text for c, text in zip(concerns, result["why_recommended"]))
    assert "do not explicitly confirm suitability" in result["why_recommended"][0]


def test_generic_word_overlap_is_not_concern_evidence():
    row = listing("This skin cream is not a brightening product.")
    assert concern_evidence(row, ["dry skin"])[0]["status"] == "not_stated"
    assert concern_evidence(row, ["brighten my face"])[0]["status"] == "listing_mention"
    assert concern_evidence(listing("Cotton pocketed shirt."), ["pockets"])[0]["status"] == "listing_mention"


def source_with_raw(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw = {"parent_asin": "B000000001", "details": {"Skin type": "Dry", "Item Volume": "50 ml"}}
    line = (json.dumps(raw) + "\n").encode()
    archive = raw_dir / "fixture.jsonl.gz"
    archive.write_bytes(gzip.compress(line))
    row = product()
    row["selected_observation"]["record_sha256"] = hashlib.sha256(line).hexdigest()
    row["observations"][0]["record_sha256"] = hashlib.sha256(line).hexdigest()
    _, run, manifest = make_source(source_dir, [row])
    manifest["sources"][0].update(filename=archive.name, sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), expected_rows=1)
    (run / "manifest.json").write_text(json.dumps(manifest))
    return source_dir, raw_dir, tmp_path / "enriched", archive


def test_enrichment_publishes_valid_immutable_subset_and_reuses_identical_run(tmp_path):
    source_dir, raw_dir, output, _ = source_with_raw(tmp_path)
    original = (source_dir / "current.json").read_bytes()
    first = publish_enrichment(source_dir, raw_dir, output)
    assert first["products"] == first["products_enriched"] == 1
    with validated_source(output) as source:
        row = next(source.rows())[2]
        assert row["attributes"]["volume"]["value"] == "50 ml"
        assert source.manifest["derivation"]["source_import_id"]
    assert publish_enrichment(source_dir, raw_dir, output) == first
    assert (source_dir / "current.json").read_bytes() == original


def test_archive_tampering_never_publishes_pointer(tmp_path):
    source_dir, raw_dir, output, archive = source_with_raw(tmp_path)
    archive.write_bytes(gzip.compress(b'{}\n'))
    with pytest.raises(CatalogError, match="checksum"):
        publish_enrichment(source_dir, raw_dir, output)
    assert not (output / "current.json").exists()


@pytest.mark.parametrize("title,concern", [
    ("Fanola Curly And Wavy Hair Shampoo", "curly hair"),
    ("Moisturizer for Sensitive, Dry & Normal Skin", "dry skin"),
])
def test_coordinated_type_wording_keeps_literal_evidence(title, concern):
    item = concern_evidence(listing(title), [concern])[0]
    assert item["status"] == "listing_mention"
    assert all(e["text"] in title for e in item["evidence"])

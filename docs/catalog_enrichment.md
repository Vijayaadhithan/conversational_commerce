# Listing enrichment and concern explanations

Run enrichment after the original ETL subset is complete. It keeps the same product
IDs, order, prices, broad families and selected observations. It adds source-backed
attributes to a separate immutable snapshot; it does not expand the 20,000-row sample.
The original normalized exports and raw archives are untouched.

```sh
.venv/bin/python scripts/enrich_catalog.py
.venv/bin/python scripts/import_catalog.py --etl-output-dir data/enriched --stage
# Use the import_id printed by the preceding command:
.venv/bin/python scripts/build_search_index.py --catalog-import-id IMPORT_ID
.venv/bin/python scripts/verify_catalog.py --etl-output-dir data/enriched
.venv/bin/python scripts/verify_search_index.py
```

Staging leaves the serving catalog and search generation in place. The index builder
reuses compatible unchanged vectors, verifies the new generation and publishes both
pointers in one transaction. A failed build retains completed batches for resume;
use both `--catalog-import-id IMPORT_ID` and `--resume-build BUILD_ID`. A concurrent
catalog change prevents publication. Existing snapshots remain available for rollback.

Set `CATALOG_ETL_OUTPUT_DIR=data/enriched` for subsequent default import/verification.
`CATALOG_ENRICHMENT_SOURCE_DIR` independently identifies the original ETL output
(default `../conversational_commerce_etl/data/processed`). Source changes require
another enrichment run, staged import and index build.

The pipeline verifies the original subset, compressed archive checksums and row
counts, each selected raw row's SHA-256, and its product ID. It reads only the chosen
observation for that product. The derived run identity includes the source manifest
identity and enrichment code hashes. Its manifest records derivation and checksums;
`enrichment_report.json` records coverage before/after, remaining unknowns and examples.
Publication validates the existing v1 JSON Schema; no upstream schema change is needed.

## Added metadata

- Explicit detail aliases (including `Skin type`), volume and style.
- `benefits`, retaining the original `product_benefits` claim for compatibility.
- Labeled ingredient lists, active ingredients, skin/hair types and sizes in listing
  text, retaining the source field. Marketing ingredient mentions are not complete
  ingredient lists; `featured_ingredients` is separate from `ingredients`.
- Conservative product-type terms from titles within the existing broad family.
  Ambiguous multi-type titles stay unknown.
- Explicitly listed size/color options, separate from the selected listing's size,
  color, scent, volume and style. Options never form invented size/color combinations.

Missing or contradictory candidate values stay unknown. Extraction rejects negated
type statements, unlabeled ingredient prose and package dimensions as sizes. Existing
explicit attributes are preserved. Values are historical listing claims, not verified
medical suitability, ingredient completeness or live variant availability. The source
contains parent products, so `variant_metadata.purchasable_variants` and `availability`
remain null. Size systems and units are not converted, and strict filters still use
exact case-insensitive values; unknowns and option lists cannot satisfy a selected-size
filter. Coverage improvements do not measure recommendation quality.

## Explanations

`POST /products/search` accepts optional `concerns` (up to 20). Commerce supplies the
saved concerns automatically. Each result has a `concern_evidence` entry per concern,
with original user wording, source excerpts, matching topics and one of:

- `listing_mention`: related wording was found, including any negation in its context.
- `not_stated`: no supported matching excerpt was found.
- `needs_clarification`: wording such as "less chemical" has no definite attribute.

Matching searches full title, attributes, features and description before returning
bounded excerpts. It uses exact phrases and a small, explicit set of topic aliases,
including common misspellings. It does not infer benefits from ingredients or treat
word overlap as proof of suitability. `why_recommended` presents separate concerns
with quotes and source paths, alongside budget and mandatory-attribute explanations.
The matching is conservative and can miss paraphrases; human-reviewed recommendation
quality evaluation is still a separate next step.

## Recommendation-quality revision

The second enrichment version groups title aliases for the same product (for
example, face cream/moisturizer), recognizes jewelry and running-shoe titles, and
keeps named accessories distinct. It recovers missing title recipient claims,
coordinated skin/hair types, concise fabric compositions, and labeled ingredient
lists, including headings separated from their contents. Marketing headings and
unlabeled ingredient mentions do not become ingredient lists. Multiple distinct
product types remain unknown rather than being reduced to a single product.

Disagreeing fabric percentages produce the derived warning
`conflicting_material_composition`. They do not choose an inferred composition. Explicit
upstream claims remain preserved, but a material filter excludes listings with this
warning. The original ETL `conflicting_fields` import gate remains unchanged.

Concern evidence now also has `support`: `positive`, `negated`, `mixed`, `weak`, or
`unknown`. This describes the wording, not clinical effectiveness or verified
product suitability. Contradictory excerpts and keyword-heavy text are explained
explicitly. Brightening matching includes brightness, radiance and even-tone wording;
there is still no inference from ingredients to outcomes.

After candidate retrieval and optional model reranking, stable evidence ordering
prefers positive concern evidence and fewer unrequested bundle, personalization,
child-sizing or peel-format cautions. Partial matches are retained with visible
cautions, rather than silently turning soft concerns into filters. Ordering happens
before the requested result limit. The model's original rank and score remain in
`ranking`; `evidence_rank` records the final order. This works with `rerank=false`
as well. Requests with neither `product_goal` nor `concerns` retain their previous
ranking behavior.

For an explicit single color in a recognized product goal, identity checks require
that color in the title or selected color/material metadata. A different selected
color cannot satisfy it. Generic prose advertising other shades is insufficient;
opaque shade names without supporting color wording may therefore lose recall.
No available-size or color-option list is treated as a selected variant.

Rebuild and publish the existing 50,000 products using the staged workflow:

```sh
.venv/bin/python scripts/enrich_catalog.py --etl-output-dir data/scaled --output-dir data/enriched_quality
.venv/bin/python scripts/import_catalog.py --etl-output-dir data/enriched_quality --stage
.venv/bin/python scripts/build_search_index.py --catalog-import-id IMPORT_ID
.venv/bin/python scripts/verify_catalog.py --etl-output-dir data/enriched_quality
.venv/bin/python scripts/verify_search_index.py
.venv/bin/python scripts/evaluate_listing_quality.py
```

The replay cases in `evals/listing_quality.json` preserve the requests and prior IDs
from the conversation repair report, including more-options exclusions. The script
verifies returned IDs against SQL constraints and quotes against source fields.
This is regression evidence, not an independent human relevance evaluation.

# Recommendation quality and catalog enrichment

20 October 2026. Scope: product matching, listing explanations, and source-backed
metadata for the existing 50,000 products. AI inspection and automated regression
checks; no human review.

## Implemented changes

- Recognized product identity gates now cover necklaces, serums and sunscreen in
  addition to the previous types. Shoe-name matches exclude socks and running
  shorts, and necklace-name matches exclude display holders and extenders.
- A named color must occur in the title or selected color/material metadata.
  Marketing about other shades cannot establish the selected product's color;
  a conflicting selected color is excluded.
- Evidence ordering happens after retrieval/model scoring and before the result
  limit. Positive concern wording takes precedence over missing or weak evidence.
  Unrequested sets, personalized items, child-sized items, and chemical/acid peel
  formats receive visible cautions and lower priority. These are partial-match
  preferences, not automatic assumptions about safety or the user's recipient.
- Explanations distinguish positive wording, negation, contradictory claims, weak
  keyword inventories and unknown evidence. They recognize brightness/radiance and
  coordinated skin/hair types, retaining exact excerpts and source paths.
- Explicit “Any more choices?” requests use the existing seen-product exclusion
  path directly, avoiding model misclassification that repeated the same cards.
- The reranker sees concise metadata before long marketing prose. Original RRF and
  model ranks/scores remain available alongside the final evidence rank. Cache keys
  include the new quality code, preventing reuse of stale recommendation outputs.

## Catalog changes

The selected product IDs, ordering, prices, observations, broad categories and full
listing text were compared with the previous enriched catalog and preserved.
13,560 records changed through derived attributes or quality warnings. No new
products, prices, inventory, purchasable variants or ingredient-based outcomes were
invented. Enrichment reads checksum-verified selected source observations.

| Source-backed attribute | Previous coverage | New coverage | Net increase |
|---|---:|---:|---:|
| Product type | 14,080 | 21,243 | 7,163 |
| Department/recipient | 12,490 | 13,922 | 1,432 |
| Material | 8,564 | 11,838 | 3,274 |
| Skin type | 6,975 | 7,097 | 122 |
| Ingredients | 173 | 231 | 58 |
| Inactive ingredients | 0 | 6 | 6 |

These counts are out of all 50,000 products, not category-specific completeness
rates. Hair type, selected size, volume, and variant-option coverage did not increase
in this revision. Ingredients remain especially sparse. Sixty-two listings received
an explicit composition-conflict warning; strict material filters exclude those
listings. The existing ETL critical-conflict import check remains intact.

Enrichment completed in 70.56 seconds with reported peak RSS 70,369,280 bytes.
The final immutable run is `c90e11a1685ba51d1740`; catalog import is
`4b4a294fe0aaa3e6a214db2f0fd67937b9a37eed9fcc85a9b14c5da5d8e7be17`.
Intermediate extraction snapshots were not activated. Existing serving generations
are retained for rollback.

## Validation

The final automated suite passed 279 tests, including isolated PostgreSQL and Redis
integration checks. One existing Starlette/httpx deprecation warning remains.
The regression tests cover evidence ordering before truncation, strict eligibility,
contradictory materials, selected shades, source provenance, unsupported ingredient
headings, preservation of explicit bundle/recipient requests, and repeated-option
exclusion for conversational wording.

The staged catalog and search were atomically published and independently verified
at 50,000 products. Build ID:
`a8b2a266655f6315595355f23dbc667d8aef93ec6f92c350bfb61fcb60350180`.
The build generated 12,273 changed embeddings and reused 37,727 compatible vectors.
The local `.env` catalog source now points to `data/enriched_quality`.

Eight real searches returned 31 product cards. All returned cards passed SQL
eligibility and exact-source excerpt checks. The seven historical replay requests
kept their original filters and more-options exclusions; the additional shoe case
uses a deliberately higher budget.

| Known issue | Final search observation |
|---|---|
| Undefined “less chemical” | The previously flagged chemical peel is absent from the top five. All five still correctly mark this preference as requiring clarification. |
| Cotton shirts with pockets | The known contradictory-composition and keyword-only pocket listings are absent from the top five. Returned listings have positive pocket wording. |
| Silver necklaces | Previously returned sets and the personalized nameplate item are absent from the top five; returned titles/metadata support silver necklace identity. |
| Red lipstick / more options | The previously flagged sets and the unsupported selected shade are absent. Selected color claims are also shown in explanations when they supply the matching evidence. |
| Handbag | The previously flagged child handbag is absent from the top five. |
| Fragrance-free brightening | Five of five returned listings have positive related wording, versus three of five identified in the previous report. One previously missed listing is now recognized through its brightness/radiance wording. |
| Women's running shoes below $75 | Still no qualifying products; unrelated accessories or children's shoes are not substituted. |
| Women's running shoes below $150 | One $149 historical listing is recovered through its explicitly stated title recipient. This is additional coverage validation, not a like-for-like budget comparison. |

Seven real conversations (17 turns) also returned HTTP 200 with the expected
clarify/recommend/compare/multi-task actions. Captured state checks confirmed
independent task budgets, task restoration, exclusion of seen products, and explicit
resolution of the fragrance-free preference. This complete run used the final code.
The preceding run exposed the “Any more choices?” repetition, which was repaired
and covered by this rerun and regression tests.

These are targeted observations, not new overall conversation-success scores or a
controlled relevance metric. Ranking, metadata and evidence matching changed
together. Source wording does not establish product effectiveness.

## Limits

This is a deterministic, source-backed improvement to a small set of known cases,
not a claim of perfect classification or recommendation quality. Opaque shade names
without explicit color wording may be excluded. Soft-concern matching and keyword
inventory detection remain heuristic. A product with listing wording is not proven
effective or suitable for a person. Undefined preferences such as “less chemical”
still need clarification. Missing products or unavailable source metadata cannot be
recovered through enrichment alone; the original constraints remain in force.

## Reproduction and artifacts

See [enrichment workflow](../docs/catalog_enrichment.md) for staged import/index
publication commands. Regression fixtures and runners:

- [Search requests](../evals/listing_quality.json) and [replay script](../scripts/evaluate_listing_quality.py).
- [Conversation cases](../evals/conversations_quality.json).
- [Metadata comparison](../data/evaluation/quality_metadata_comparison.json).
- [Catalog preservation checks](../data/evaluation/quality_catalog_checks.json).
- [Final search capture](../data/evaluation/listing_quality_final.json) and [known-issue checks](../data/evaluation/quality_known_issue_checks.json).
- [Conversation capture](../data/evaluation/conversations_quality_verified.json) and [state checks](../data/evaluation/quality_conversation_checks.json).

The search fixtures replay seven known requests from the previous report and add a
higher-budget women's running-shoe check. They are not a held-out evaluation set.

# Local product reranking

Product search now uses the installed `mixedbread-ai/mxbai-rerank-large-v2`
cross-encoder after hybrid retrieval. Its model-provided Sentence Transformers
modules and chat template handle pair scoring; there is no custom remote model
code or model download in a search request. See the
[model card](https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2) and
[Sentence Transformers cross-encoder usage](https://www.sbert.net/docs/cross_encoder/usage/usage.html).

## Request and configuration

```json
{
  "query": "running shoes",
  "filters": {"domain": "fashion", "family": "footwear", "max_price": "75.00"},
  "limit": 5,
  "rerank": true
}
```

`rerank` defaults to true and requires a JSON boolean. Set it to false to
explicitly return the RRF baseline. CLI equivalents:

```bash
.venv/bin/python scripts/search_products.py 'running shoes' \
  --filters '{"domain":"fashion","family":"footwear","max_price":"75.00"}'

.venv/bin/python scripts/search_products.py 'running shoes' \
  --filters '{"domain":"fashion","family":"footwear","max_price":"75.00"}' --no-rerank
```

`PRODUCT_RERANK_CANDIDATES=20` controls the bounded pool, with an allowed range
of 20-50. Existing `RERANK_MODEL_PATH`, `RERANK_DEVICE`, `RERANK_BATCH_SIZE`, and
`RERANK_MAX_LENGTH` configure the local engine. Defaults remain auto device,
batch 2, and 512 tokens per pair. The local 1.54B model is loaded lazily once per
process; API search and `/rerank` share that instance and their generation lock.
Use a single server worker on this Mac. Separate CLI processes load separate
instances, so avoid running model checks concurrently with serving requests.

When requested reranking fails because of missing weights/libraries, inference,
or invalid output, search returns an explicit 503. It does not claim reranking
was applied or silently switch to RRF. Empty eligible results remain empty and
skip reranking entirely. Core dependency setup continues to leave weights and
optional heavy inference libraries separate; follow the README reranker setup
instructions when needed. No migrations or re-embedding are required for this
integration.

## Candidate and constraint behavior

1. SQL enforces all structured mandatory filters before keyword/vector limits.
2. RRF orders the union of the two top-50 candidate paths.
3. Search hydrates up to the configured reranking pool from the pinned catalog
   snapshot, then closes the PostgreSQL read transaction.
4. The cross-encoder scores all eligible pool members; final selection happens
   after reranking, so a product below the requested page can be promoted.
5. Only those same product records can appear in the result.

The reranker cannot invent IDs, change prices/attributes, restore excluded IDs,
or admit products with unknown required claims. Its output must contain exactly
one finite score per candidate index, with no duplicates, missing rows, invalid
indices, or mismatched returned text. Equal scores preserve the previous RRF
order. Model-returned text is never used to replace catalog data.

The generation/build metadata returned with the result still identifies the
catalog snapshot pinned at retrieval time, even if a newer generation is
published while inference runs. Mandatory filters remain structured; query text
alone is not a budget/attribute parser.

## Input and output

`app/product_reranking.py` selects listing text under a separate
`commerce-rerank-v1` format: title first (up to 768 UTF-8 bytes), then broad
category, brand, features/descriptions, and available attribute claims. The
selected document is bounded at 4,000 UTF-8 bytes. Original records, claims, and
provenance remain unchanged. These limits select a model representation; they
are not product-data edits.

The model can further truncate document content to its pair token budget. For
product search, the effective pair window is at least the full query's UTF-8
byte length plus 256 tokens of template/suffix headroom, or the configured pair
limit if larger. With accepted queries bounded at 1,000 UTF-8 bytes, this keeps
the query in the front of the model-provided template rather than silently
shortening it. The tokenizer must use right truncation. The raw `/rerank`
endpoint retains its existing configured pair-limit behavior.

Result `ranking` fields retain keyword/vector/RRF signals and add `rrf_rank`,
`rerank_rank`, and `rerank_score`. The top-level `reranking` object exposes
whether scoring was applied, candidate count, configured model revision, device,
effective pair window, document version, and observed scoring/load duration.
Scores are raw relevance log-odds, not calibrated suitability probabilities.
No minimum-score threshold or medical-safety judgment is inferred.

## Live checks and limitations

```bash
CATALOG_TEST_ADMIN_URL=postgresql:///postgres .venv/bin/python -m pytest -q
.venv/bin/python scripts/check_product_reranking.py
```

The live checker compares baseline and reranked results for 12 queries. It
checks candidate-ID preservation, ordering by finite scores, exact active build,
and category/price/attribute constraints. Its report is local and ignored by Git:
`data/search/reranking_report.json`. There are no human relevance qrels, Recall,
NDCG, or general recommendation-accuracy claims.

In the first Apple MPS run, the first of 10 nonempty searches took 6.13 seconds
including cold model load. The remaining nine took approximately 1.64-2.55
seconds warm, with a 2.32-second warm median. The two zero-result cases skipped scoring. These
are observations on this Mac/sample/configuration, not a throughput guarantee.

For “running shoes”, the former top shoelace listing moved to rank 18 and an
actual running shoe ranked first. For “razor blades”, shaving refills ranked
first, but a utility-style single-edge blade remained highly ranked. The model
does not fix every source-category error. Product-type/variant clarification
and human-judged evaluation are still required; the sampled catalog also has
limited age/size/stock metadata. Listing excerpts are not independently verified
explanations of suitability. Conversation preference state and grounded reply
generation remain the next integration step.

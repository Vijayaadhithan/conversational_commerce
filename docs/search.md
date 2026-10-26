# Product embeddings and Qdrant retrieval

Product search retrieves candidates from Qdrant and reads facts from the active PostgreSQL catalog. It does not modify the ETL
data, conversation transcripts, or preference state. No model download is part
of setup or index generation. Run bulk generation when chat/reranking is idle;
the API shares its in-process generation lock across chat, reranking, and query
embedding, while the standalone build runs in a separate process.

## Build and verify

```bash
# PostgreSQL and Redis; this does not download models
bash scripts/setup.sh --with-services
docker compose -f compose.qdrant.yml up -d
.venv/bin/python scripts/init_catalog.py
.venv/bin/python scripts/build_search_index.py
.venv/bin/python scripts/verify_search_index.py
.venv/bin/python scripts/build_qdrant_index.py --verify-only
```

Settings are in `.env`: `EMBEDDING_MODEL=embeddinggemma:latest`,
`EMBEDDING_BATCH_SIZE=32`, and `EMBEDDING_TIMEOUT_SECONDS=120`. The installed
model is required. Its immutable Ollama digest is recorded in every build's
configuration and checked around each embedding operation. Tag changes fail
instead of mixing vectors from different models. The current storage schema
requires 768 dimensions and the text contract uses EmbeddingGemma prompts.

Build identity hashes the catalog import ID, model name/digest, and input
contract (including its producer-file SHA-256) and Parquet storage version.
Completed batches are fsynced and atomically renamed before their metadata commits to a
building generation. On failure they remain available for resume, while the
previous active generation remains unchanged. Only a complete verified build
is marked ready and published. Writer locks prevent simultaneous bulk builds.
Publication checks that the active catalog has not changed. If a new catalog is
imported, search returns 503 until a matching build is available; it never mixes
old vectors with new product records. Old generations are retained for deliberate
reuse. Explicit retention and deletion commands protect the active generation;
unfinished builds are preserved for resume unless explicitly selected for cleanup.
See [embedding_lifecycle.md](embedding_lifecycle.md).

Tables: `commerce.search_builds`, `commerce.search_documents`, and
`commerce.search_active`. Product/build foreign keys preserve generation
identity. Embeddings are normalized and validated for count, finite values,
nonzero norm, and dimension. Parquet stores float32 arrays of 768 values; SQL stores
input hashes and batch references only. `SEARCH_ARTIFACT_DIR` defaults to
`data/search/embeddings`; use a dedicated directory per database. Verification checks
each batch checksum and its product/input identity before using its vectors. Missing
or corrupt files fail explicitly. Qdrant defaults to exact dense search; ANN is opt-in.

Across catalog snapshots, selected-text hashes and matching model/input contracts
allow reuse of vectors from retained ready generations. New or changed inputs
are embedded; current metadata and keyword text are always regenerated. Deleted
products are excluded by the new snapshot. A complete empty snapshot withdraws
every product from active search. Full/selective refresh and protected physical
cleanup are supported by the lifecycle commands linked above.

## Versioned text selection

`app/embedding_text.py` defines the contract. The document prompt uses the
title and listing text; the query prompt uses the model's retrieval task.
These follow the [EmbeddingGemma model card](https://huggingface.co/google/embeddinggemma-300m#prompt-instructions).

The selected embedding input has a 1,536 UTF-8 byte budget, including its prompt.
Titles have a 512-byte budget. Category, brand, and explicit attribute claims
precede features and descriptions; individual attribute strings are limited to
192 bytes. The lexical body selects up to 8,192 bytes. Selection preserves UTF-8
boundaries, records whether embedding text was clipped, and keeps the complete
normalized product unchanged in catalog storage. Prices and stock are not added
to the semantic text. These bounds are document-representation choices, not
silent truncation of a user's query or of the catalog record.

Ollama receives `truncate:false`, so exceeding the model context window fails
instead of silently cutting additional tokens. See the
[Ollama embedding API](https://docs.ollama.com/api/embed). Each selected input is
hashed; verification regenerates it from the catalog and checks stored text,
source-line hashes, counts, and vector norms. This validates build integrity,
not the semantic correctness of the model's embedding values.

## Search contract

```json
{
  "query": "moisturizer for dry skin",
  "filters": {
    "domain": "beauty",
    "family": "skin_care",
    "max_price": "30.00",
    "attributes": {"fragrance_free": true}
  },
  "limit": 5,
  "rerank": true
}
```

Send to `POST /products/search`, or use:

```bash
.venv/bin/python scripts/search_products.py 'moisturizer for dry skin' \
  --filters '{"domain":"beauty","family":"skin_care","max_price":"30.00","attributes":{"fragrance_free":true}}'
```

Query text must fit 1,000 UTF-8 bytes; limits are 1-20 products. Invalid fields,
price ranges, and unsupported attribute filters return 422 from HTTP. SQL uses
bound values. Database/service/build errors return 503 without connection-string
disclosure. Search uses a read-only repeatable-read transaction to pin one
catalog/build for the entire request.

Mandatory filters are structured: domain, family, exact case-insensitive brand,
minimum/maximum historical USD price, explicit attribute values, and up to 100
excluded product IDs. Attribute text matches the entire observed value,
case-insensitively; it does not split ambiguous compound labels. Only
`fragrance_free` accepts a boolean. Unknown, missing, and conflicting values are
not inferred as a match. Stock and detailed purchasable variant availability are
unknown in this parent-product catalog, so corresponding filters are unsupported.

The query text supplies relevance; it is not currently a natural-language filter
parser. A budget or mandatory attribute mentioned only in query text is not
enforced automatically. The response exposes `filters_applied` and
`constraints_source: structured_filters` for downstream conversation planning.

Both Qdrant branches use identical eligible IDs before their top-50 limits:

1. Qdrant native `qdrant/bm25`, English tokenization and IDF.
2. Dense cosine retrieval with `QDRANT_EXACT=1`; set it to `0` only after evaluating ANN.
3. Reciprocal rank fusion with constant 60 and product-ID tie breaks.
4. Hydration from the same pinned PostgreSQL catalog snapshot.
5. Configured reranking of eligible candidates, followed by listing-evidence ordering.

Qdrant is required for online search and index publication. Remove the retired
`SEARCH_BACKEND` setting; a legacy `postgres` value is rejected with an explicit error.
`QDRANT_URL`, optional `QDRANT_API_KEY`, and `QDRANT_COLLECTION_PREFIX` configure the
service. The Compose file pins the server, binds it to loopback, and retains a named
storage volume. Stop it with `docker compose -f compose.qdrant.yml down`, without volumes.

PostgreSQL computes exact eligibility first. `QDRANT_MAX_ELIGIBLE` bounds this set at
100,000 by default; over-limit requests fail rather than silently truncating results.
Collections are named from the index contract and full build ID. Readiness validates
the registered collection, vector contract, and point count before any cache hit.
There is no automatic or selectable PostgreSQL online fallback.

The standalone exporter can rebuild Qdrant from retained Parquet embedding snapshots without
model calls: `.venv/bin/python scripts/build_qdrant_index.py`. A damaged ready collection
is not repaired in place; export to a new `QDRANT_COLLECTION_PREFIX`, verify it, and then
restart the API with that prefix. Keep the previous verified generation for rollback.

The RRF
candidate pool is bounded and does not guarantee that every relevant product
appears in the final ranking. Similarity/RRF scores are not calibrated relevance
or suitability probabilities, and semantic search can return weak matches.

Results retain listing claims, historical price context, selected observation
provenance, and contiguous listing excerpts with matched terms and original
field references. Excerpts show lexical overlap, not a medical explanation or
independent confirmation of suitability. Products with no mandatory matches
return an empty result; the backend does not relax constraints to fill a page.

## Cache and warm-up

`SEARCH_CACHE_REDIS_URL` enables an optional Redis result cache. Defaults are a
300-second TTL, 128 entries, and a 256 KiB payload limit. Keys include the catalog
and search generation, model identity, query, constraints, and ranking-code identity.
Readiness checks run before cache hits. Malformed entries and Redis outages become
misses; namespace limits do not change the server-wide Redis configuration.

`SEARCH_WARMUP=1` probes embedding and reranking models before API startup finishes.
It shifts their initial loading cost to startup and does not precompute queries or
warm the chat planner. See [operations](operations.md) for readiness and monitoring.

Conversation searches supply `product_goal` for listing-identity filtering before
both retrieval branches. Direct search clients may supply it too; the value forms
part of the cache key. These checks identify product types, not verified benefits
or safety claims.

## Evaluation and remaining work

```bash
CATALOG_TEST_ADMIN_URL=postgresql:///postgres QDRANT_TEST_URL=http://127.0.0.1:6333 .venv/bin/python -m pytest -q
.venv/bin/python scripts/check_search.py
.venv/bin/python scripts/check_product_reranking.py
```

Tests use fake vectors to exercise protocol validation, source-contract bounds,
resume, publication, snapshot isolation, hard filters, null claims, rank fusion,
API validation, and query parameterization against a temporary PostgreSQL DB and isolated Qdrant collections.
The baseline live smoke script explicitly disables reranking and uses the real
embedding model/catalog, writing a local report to
`data/search/smoke_report.json`. Its category/price/attribute checks verify
constraints and execution. They are not human relevance judgments or qrels and
must not be reported as recommendation accuracy, Recall, or NDCG.

The existing reranker is connected; see [product_reranking.md](product_reranking.md).
Conversation state, listing evidence, and bounded Redis caching are implemented.
Human-judged relevance and complete-journey evaluation remain separate from mechanical checks.

The original baseline smoke run exposed relevance limitations: a shoelace listing
ranked above a running shoe, and a scraper-blade listing appeared for razor
blades. Broad source-derived families are noisy. These observations are retained
in the local smoke report and motivated the reranker integration. Product-type
evaluation is still needed;
passing structured-filter checks does not establish correct product suitability.

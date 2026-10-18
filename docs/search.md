# Product embedding and retrieval baseline

Product search reads the active PostgreSQL catalog. It does not modify the ETL
data, conversation transcripts, or preference state. No model download is part
of setup or index generation. Run bulk generation when chat/reranking is idle;
the API shares its in-process generation lock across chat, reranking, and query
embedding, while the standalone build runs in a separate process.

## Build and verify

```bash
# Matching PostgreSQL + pgvector files; this does not download models
bash scripts/setup.sh --with-services
.venv/bin/python scripts/init_catalog.py
.venv/bin/python scripts/build_search_index.py
.venv/bin/python scripts/verify_search_index.py
```

Settings are in `.env`: `EMBEDDING_MODEL=embeddinggemma:latest`,
`EMBEDDING_BATCH_SIZE=32`, and `EMBEDDING_TIMEOUT_SECONDS=120`. The installed
model is required. Its immutable Ollama digest is recorded in every build's
configuration and checked around each embedding operation. Tag changes fail
instead of mixing vectors from different models. The current storage schema
requires 768 dimensions and the text contract uses EmbeddingGemma prompts.

Build identity hashes the catalog import ID, model name/digest, and input
contract (including its producer-file SHA-256). Completed batches commit to a
building generation. On failure they remain available for resume, while the
previous active generation remains unchanged. Only a complete verified build
is marked ready and published. Writer locks prevent simultaneous bulk builds.
Publication checks that the active catalog has not changed. If a new catalog is
imported, search returns 503 until a matching build is available; it never mixes
old vectors with new product records. Old generations are retained for deliberate
reuse; no retention cleanup is performed automatically.

Tables: `commerce.search_builds`, `commerce.search_documents`, and
`commerce.search_active`. Product/build foreign keys preserve generation
identity. Embeddings are normalized and validated for count, finite values,
nonzero norm, and dimension. Database vectors use float32 `vector(768)`; no
half-precision reduction or approximate index is used in this baseline.

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
  "limit": 5
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

Both retrieval paths use identical eligible IDs before their top-50 limits:

1. PostgreSQL English full-text search (`plainto_tsquery`, AND semantics) with
   title-weighted `ts_rank_cd`. This is PostgreSQL text ranking, not BM25.
2. Exact cosine search over eligible pgvector embeddings. There is no ANN index
   or post-ANN filtering that could lose restrictive-filter candidates here.
3. Reciprocal rank fusion with constant 60 and product-ID tie breaks.
4. Hydration from the same pinned catalog snapshot.

See [pgvector exact search and indexing](https://github.com/pgvector/pgvector#indexing).
Exact search is a baseline for 20,000 records; larger catalogs need measured
latency and filtered-recall checks before adding approximate indexes. The RRF
candidate pool is bounded and does not guarantee that every relevant product
appears in the final ranking. Similarity/RRF scores are not calibrated relevance
or suitability probabilities, and semantic search can return weak matches.

Results retain listing claims, historical price context, selected observation
provenance, and contiguous listing excerpts with matched terms and original
field references. Excerpts show lexical overlap, not a medical explanation or
independent confirmation of suitability. Products with no mandatory matches
return an empty result; the backend does not relax constraints to fill a page.

## Evaluation and remaining work

```bash
CATALOG_TEST_ADMIN_URL=postgresql:///postgres .venv/bin/python -m pytest -q
.venv/bin/python scripts/check_search.py
```

Tests use fake vectors to exercise protocol validation, source-contract bounds,
resume, publication, snapshot isolation, hard filters, null claims, rank fusion,
API validation, and query parameterization against a temporary PostgreSQL DB.
The live smoke script uses the real model/catalog and writes a local report to
`data/search/smoke_report.json`. Its category/price/attribute checks verify
constraints and execution. They are not human relevance judgments or qrels and
must not be reported as recommendation accuracy, Recall, or NDCG.

Connecting the existing reranker, maintaining conversational requirements,
grounded user-facing explanations, and human-judged relevance evaluation remain
subsequent work. Redis caching can follow measured latency and correctness.

The first live smoke run also exposed relevance limitations: a shoelace listing
ranked above a running shoe, and a scraper-blade listing appeared for razor
blades. Broad source-derived families are noisy. These observations are retained
in the local smoke report and require product-type evaluation and reranking;
passing structured-filter checks does not establish correct product suitability.

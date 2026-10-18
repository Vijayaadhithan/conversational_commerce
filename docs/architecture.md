# Architecture and storage

## Implemented

The FastAPI app talks to Ollama's chat API. Each conversation has a UUID and an
independent transcript in SQLite. A turn's user message, assistant reply, and
memory update commit together after successful inference.

Model input contains the system prompt, an optional summary, recent turns, and
the current user message. At most six completed turns remain in the recent
window; a conservative UTF-8 byte budget can reduce that further. Older turns
update a bounded rolling summary using the same local chat model. Original
messages remain available through the paginated transcript API.

Chat and reranking share a generation lock to limit simultaneous local model
work. Use one server worker; process-local locks do not coordinate multiple
workers. This is a local development backend, without multi-user authentication,
distributed admission control, or automatic retries of ambiguous chat requests.

## Models

| Role | Model | Runtime |
| --- | --- | --- |
| Conversation | `ministral-3:8b` | Native Ollama |
| Product embeddings | `embeddinggemma:latest`, 768 dimensions | Native Ollama; catalog and query retrieval prompts |
| Reranking | `mixedbread-ai/mxbai-rerank-large-v2`, 1,543,714,304 parameters | PyTorch / Sentence Transformers, Apple MPS or CPU |

The reranker is pinned to revision
`ca7e1ee484c37c0ddd8d178a9a5c33cec575c5e6`. Its model-provided Sentence Transformers
modules and tokenizer chat template determine pair formatting and relevance
scoring. Inference uses local files only and does not execute custom remote
model code. GPU weights use float16, with batch size 2 and a 512-token pair limit
by default. CPU weights use float32. The model is a general retrieval reranker;
the project has no comprehensive fashion/beauty relevance benchmark yet.
Raw log-odds scores are retained instead of applying sigmoid, avoiding artificial
ties when strong matches saturate to 1.0 in float16.

The `/products/search` endpoint retrieves catalog products and reranks the top
20 eligible RRF candidates by default. `/rerank` ranks caller-supplied documents.
Both share one lazy local reranker and the API generation lock. Commerce-mode
conversations extract evidence-backed intent first, persist separate shopping
tasks and requirements in SQLite, and invoke filtered search for discovery and
refinement. Grounded replies retain listing evidence; direct comparisons use
saved recommendation snapshots. See [conversations.md](conversations.md).

## Catalog inputs

Only All Beauty, Amazon Fashion, and Beauty & Personal Care metadata from Amazon
Reviews 2023 are selected. Downloaded rows may overlap and need deduplication
by product ID. Preserve source provenance and unknown/missing attributes during
normalization. Image URLs are metadata, not bundled image files.

These snapshots do not establish current stock, prices, commercial rights, or
complete coverage of product variants, ages, genders, ingredients, or sizes.

## Recommended retrieval storage

Use a dedicated PostgreSQL database with pgvector for catalog records and
vectors, and Redis for bounded caches. `scripts/setup.sh --with-services` can
install PostgreSQL, matching pgvector extension files, and Redis. Creating a
dedicated database and importing the normalized subset are implemented through
`scripts/init_catalog.py` and `scripts/import_catalog.py`. Catalog migrations
enable pgvector in the chosen database. `scripts/build_search_index.py` builds
resumable model/digest/input-version-specific embeddings, then atomically
publishes a complete search generation. Redis caching is not wired into search.
Incremental embedding reuses compatible selected-text hashes across retained
ready generations. Forced full/selective refreshes build separate resumable
generations. Removed products stay outside the next active snapshot; protected
retention removes inactive vectors physically. See [embedding_lifecycle.md](embedding_lifecycle.md).

The catalog uses the `commerce` schema in its own PostgreSQL database. Immutable
import snapshots preserve each normalized JSONB product, source provenance,
unknown fields, historical prices, and the ETL manifest. Generated typed columns
provide consistent filter values. Importing uses validated client-side COPY,
transaction-scoped writer coordination, and an atomic active-snapshot pointer;
repeated imports verify and reuse existing records. The active-products view
contains only the chosen snapshot, so removed products cannot leak in from old
imports. See [catalog.md](catalog.md).

PostgreSQL can keep authoritative product filters and vectors together, using
SQL for category, brand, price, and variant constraints. It stores data on disk
and caches a working set; index and query memory still need measurement.
Redis vector sets can support a prototype, but catalog/index memory competes
with the models on a single Mac.

Two million 768-dimensional float32 vectors alone occupy about 6.14 GB, before
indexes and metadata. `halfvec(768)` is a possible storage reduction after
checking retrieval quality. Approximate search needs filtered-recall tests,
appropriate filter indexes or partitions, and iterative scans where suitable.
Mandatory constraints must be enforced in code and SQL; reranking cannot relax
them. See [pgvector](https://github.com/pgvector/pgvector) and
[Redis vector sets](https://redis.io/docs/latest/develop/data-types/vector-sets/).

The implemented 20,000-product baseline uses exact pgvector cosine distance,
PostgreSQL English full-text search with weighted title/body terms and a GIN
index, and RRF (constant 60) over up to 50 candidates per path. It uses PostgreSQL
text ranking, not BM25. Identical SQL constraints apply before both candidate
limits. An active search build must match the active catalog, local input
contract, model name, and Ollama digest. See [search.md](search.md).

Product reranking can only reorder candidates already admitted by the SQL
filters; it cannot introduce product IDs or change mandatory constraints. It
runs after hydration and after releasing the PostgreSQL read transaction.
Responses retain the pinned catalog/build, RRF ranks, reranker scores, model
revision, device, and bounded candidate count. Invalid/incomplete reranker output
fails explicitly. See [product_reranking.md](product_reranking.md).

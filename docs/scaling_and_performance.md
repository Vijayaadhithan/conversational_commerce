# Scaling, evaluation and measured optimization

See the [19 October evaluation report](../reports/commerce_evaluation_2026-10-19.md)
for measured results, AI review labels, remaining intent failures and limitations.
The subsequent [conversation repair report](../reports/commerce_fixes_2026-10-19.md)
documents the repaired failures and expanded evaluation.

Conversation searches now pass an explicit `product_goal` to product search. Its
listing-identity checks apply before both retrieval paths and form part of the cache
key. Direct `/products/search` clients may also supply this optional field; omitting
it retains their existing behavior. These checks cover named product types, not
unverified benefit or safety claims.

The first measured expansion targets 50,000 products. It retains the original
20,000 and selects additional products deterministically from eligible normalized
listings, balancing additions across source/domain/family groups. It is a development
sample, not a random sample of shopper demand or a full catalog deployment.

```sh
.venv/bin/python scripts/expand_catalog.py --size 50000
.venv/bin/python scripts/enrich_catalog.py --etl-output-dir data/scaled --output-dir data/enriched_50k
.venv/bin/python scripts/import_catalog.py --etl-output-dir data/enriched_50k --stage
.venv/bin/python scripts/build_search_index.py --catalog-import-id IMPORT_ID
.venv/bin/python scripts/verify_catalog.py --etl-output-dir data/enriched_50k
.venv/bin/python scripts/verify_search_index.py
```

Sampling verifies the existing normalized archive before selection. SQLite stores
candidate payloads and sorting on disk with a 16 MiB page cache. Enrichment also uses
a disk table and streams selected source observations, rather than materializing the
subset in Python. Import preflight checks uniqueness using a disk index with a 2 MiB
cache; PostgreSQL COPY and independent verification remain streamed. Embeddings use
bounded batches (32 by default), reuse matching input hashes, and resume incomplete
generations. Catalog and search pointers publish together; failures retain the old
serving generation. No old generation is automatically deleted.

`execution_report.json` records process peak RSS and elapsed time for sampling and
enrichment. RSS excludes the separate PostgreSQL/Ollama processes and GPU allocations.
No larger-scale memory guarantee is inferred from a 50,000-product measurement.

## Reproducing evaluations

```sh
.venv/bin/python scripts/evaluate_conversations.py --output data/evaluation/conversations_50k.json
.venv/bin/python scripts/summarize_evaluation.py --capture data/evaluation/conversations_50k.json --review evals/review_50k.json --output data/evaluation/summary_50k.json
.venv/bin/python scripts/benchmark_retrieval.py --disable-cache --output data/evaluation/latency_50k.json
.venv/bin/python scripts/benchmark_indexes.py
.venv/bin/python scripts/benchmark_retrieval.py --rerank --warmup --cache-url redis://127.0.0.1:6379/0 --output data/evaluation/cache_50k.json
```

The 12 synthetic conversations are fixed in `evals/conversations.json`. Capture uses
real local models and an isolated temporary conversation database. Reviewer labels
are separate, explicit artifacts and must match the actual returned product IDs.
AI reviews are labeled as such; no human judgments are claimed. HTTP success, task
success, product relevance and structured constraint compliance are separate metrics.
The benchmark's first request is process-cold; OS, PostgreSQL and Ollama caches are
not flushed. Sequential local timing is not concurrent production load testing.

The HNSW experiment uses a disposable temporary copy of the active vectors and
filters. It measures candidate recall against exact top-50 neighbors, query plans,
index size and construction time. It never creates an ANN index on the serving table.
Measured candidate recall is not shopper relevance. Production continues exact
filtered retrieval until an ANN configuration passes both recall and latency gates.
See the [pgvector documentation](https://github.com/pgvector/pgvector#filtering) for
post-filtering and iterative-scan behavior.

## Redis cache and warm-up

`SEARCH_CACHE_REDIS_URL` enables an optional Redis result cache. Defaults are a
300-second TTL, 128 entries, and a 256 KiB payload limit (at most 32 MiB of serialized
payloads plus Redis overhead). A Lua operation atomically bounds the owned namespace;
no server-wide memory policy is changed and other application keys are untouched.
Keys include the catalog/search generation, model digest, request text, every filter,
concerns, pagination/exclusions, reranking parameters and source-code fingerprint.
Each request still pins a ready catalog and verifies embedding model identity. Cache
hits cannot bypass readiness or reuse a previous generation. Malformed values and
Redis outages become misses; connection and read timeouts are 200 ms with no retries.
The [Redis Python client](https://redis.io/docs/latest/develop/clients/redis-py/)
handles connections; cache IO runs in the existing search worker thread.

Set `SEARCH_WARMUP=1` to load/probe embedding and reranking models before API startup
completes. `/health` includes warm-up status and duration. This consumes model memory
earlier; it shifts cold-load cost to startup rather than eliminating it. Warm-up does
not precompute customer queries, and it does not imply the chat planner is warmed.

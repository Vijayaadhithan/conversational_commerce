# Commerce evaluation and 50,000-product benchmark

Date: 19 October 2026. Review: **AI only; no human judgments**.

Follow-up: the [conversation repair report](commerce_fixes_2026-10-19.md) records the
subsequent fixes and reevaluation (original cases: 10 successful, 2 partial, 0 failed).
The measurements below are the preserved pre-repair baseline.

The 50,000-product catalog and matching embedding generation are active. Sampling,
enrichment and import validation now use disk-backed working storage. Redis caching
and model warm-up are implemented. The larger catalog improved the reviewed product
matches, but **7 of 12 conversations still failed their shopping goal**. Scaling and
latency work do not resolve those intent-handling failures.

## Recommendation quality

We ran the same [12 synthetic conversations](../evals/conversations.json), comprising
23 turns, against the 20,000- and 50,000-product catalogs using the real local planner,
retrieval and reranker. Each conversation used isolated temporary chat storage.
The planner was `ministral-3:8b`; the embedding model was `embeddinggemma:latest`;
reranking used `mixedbread-ai/mxbai-rerank-large-v2` on MPS.

One AI reviewer inspected responses and listing evidence. Conversation outcomes
consider every turn. Product grades cover the last nonempty recommendation list per
conversation: 0 = poor fit/wrong type, 1 = partial or unresolved fit, 2 = strong fit
supported by listing text. These are judgments about the stated shopping request,
not independent verification of listing claims, safety or efficacy. Explicit labels
are in [20k review](../evals/review_20k.json) and [50k review](../evals/review_50k.json).

| Measure | 20,000 | 50,000 |
|---|---:|---:|
| HTTP 200 responses | 23/23 | 23/23 |
| Fully successful conversations | 1/12 | 3/12 |
| Partially successful conversations | 4/12 | 2/12 |
| Failed conversations | 7/12 | 7/12 |
| Conversations producing recommendations | 5/12 | 5/12 |
| Strong product matches | 11/25 (44%) | 17/25 (68%) |
| Partial product matches | 10/25 | 8/25 |
| Poor product matches | 4/25 | 0/25 |
| Violations of extracted structured filters | 0/45 card occurrences | 0/45 card occurrences |

Product relevance is conditional on a list being produced; the seven failed
conversations contribute no products to that denominator. Filter compliance checks
the filters the planner actually extracted, not requirements it omitted. Neither
metric establishes successful understanding of every user request.

| Conversation | 20k outcome | 50k outcome | Finding at 50k |
|---|---|---|---|
| Facial moisturizer, dry skin, lower budget | Partial | Success | All final options support facial use and dry-skin wording within the revised budget. |
| “lightens my face”, “kess chemincal”, request products | Partial | Partial | Natural recommendation wording works; the undefined preference remains unresolved for all five products. |
| Curly-hair shampoo, lower budget | Partial | Success | Relevant shampoos replace the earlier irrelevant crochet braid. |
| Fragrance-free moisturizer, then allergy question | Partial | Partial | Relevant moisturizers replace the earlier clay mask, but the allergy follow-up gets generic clarification. |
| Red lipstick, then more options | Fail | Fail | Intent validation rejects an explicit single-product request. |
| Razor blades | Fail | Fail | Intent validation rejects an explicit single-product request. |
| Crossbody handbag, lower budget | Fail | Fail | Product goal is rejected, so the follow-up lacks usable context. |
| Running shoes for adult women | Fail | Fail | Repeated recipient clarification despite the recipient already being stated. |
| Cotton shirt for adult men, then comparison | Fail | Fail | Initial request fails; comparison has no displayed products. |
| Silver necklace | Success | Success | Relevant options; sets and personalized items receive partial product grades. |
| Shampoo and moisturizer, switch task, lower budget | Fail | Fail | Multi-product clause validation fails and subsequent task switching cannot recover. |
| Moisturizer under $0.01 | Fail | Fail | Asks for product type instead of explaining no matches. |

Explanation quality still has visible gaps. For example, `amazon:B07GFRYCQ2`
has “Curly And Wavy Hair Shampoo” in its title but receives a `not_stated` explanation
for curly hair. In the first dry-skin turn, `amazon:B07N5B8WWR` has comma-separated
“Sensitive, Dry & Normal Skin” wording that the evidence matcher misses. The final
dry-skin list has supporting evidence, so this case receives a successful shopping
outcome with that explanation limitation recorded.

This is a small, synthetic, unblinded, single-run comparison reviewed by the same AI
that implemented the changes. It is not a human satisfaction study, statistical
generalization, or corpus-wide relevance/recall benchmark. Model variability and
catalog composition may affect results. No planner repair was included in this
scaling/performance change.

## Scale, memory and metadata

The deterministic sample retained all original 20,000 IDs and added 30,000 eligible
products, balancing additions across source/domain/family groups. Selection scanned
1,967,487 normalized records, of which 300,541 were eligible. Existing local source
archives were reused; no new dataset download was needed.

| Stage | Measured work | Elapsed | Process peak RSS |
|---|---|---:|---:|
| Sampling | Retain 20k, select 30k additional records | 96.07 s | 99.1 MiB |
| Enrichment | 50k records; 23,315 enriched | 66.44 s | 66.9 MiB |
| Embedding generation | 29,904 new; 20,096 reused vectors | 618.88 s | Not measured |

Sampling and enrichment use SQLite with a 16 MiB page cache. Import preflight uses
a disk uniqueness index with a 2 MiB cache; COPY and independent verification remain
streamed. Embeddings retain the existing bounded 32-record batch and input-hash reuse
path. Catalog and search generations publish together. Both independent catalog and
search verification confirmed 50,000 records. Old generations are retained.

RSS is the Python process high-water mark, excluding PostgreSQL, Ollama and GPU
allocations. The enrichment timing comes from an identical measured rerun, not the
initial run. No memory bound for a much larger catalog is inferred. The existing
embedding text contract clipped 13,254 inputs; retrieval quality for those inputs
has not been separately assessed.

Coverage below counts records with explicit metadata across the full 50k sample,
not percentages within only applicable beauty or fashion categories:

| Field | Before enrichment | After enrichment |
|---|---:|---:|
| Ingredients | 0 | 173 |
| Skin type | 6,467 | 6,975 |
| Hair type | 4,054 | 4,128 |
| Product type | 0 | 14,080 |
| Size | 1,794 | 2,450 |
| Volume | 0 | 5,822 |

Coverage remains sparse, especially for ingredients and purchasable variants.
Unknown fields stay unknown. Listed options are not verified purchasable variants;
historical prices and listing claims do not establish current availability.

## Retrieval, Redis and warm-up

Measurements used an Apple M5, 10 logical CPUs, 24 GiB RAM, local PostgreSQL 16 with
pgvector 0.8.7, local Ollama, an MPS reranker and local Redis. Requests were sequential;
this is not a concurrent load test. “Cold” means a fresh benchmark process, without
flushing OS, database or Ollama caches.

| Workload | Requests | Median | p95 |
|---|---:|---:|---:|
| 20k retrieval, no reranking or result cache | 36 | 150.4 ms | 362.1 ms |
| 50k retrieval, no reranking or result cache | 36 | 154.4 ms | 192.0 ms |
| 50k reranking, fresh process, no warm-up/cache | 12 | 2.412 s | 6.921 s |
| 50k reranking, warmed, Redis misses | 12 | 2.484 s | 2.929 s |
| 50k reranking, repeated requests, Redis hits | 24 | 13.5 ms | 17.5 ms |

The 20k/50k retrieval medians are similar in this sample. Different warmth and small
sample size prevent attributing the p95 improvement to catalog expansion. Warm-up
took 2.795 seconds in the cache benchmark. It moves model-loading work to startup;
it does not eliminate query reranking or warm the chat planner. Full conversation
latency also includes planner work and is not represented by search-only timings.

All 12 miss results had identical product ID lists on both subsequent cache hits.
The benchmark deliberately repeats queries, producing 24 hits from 36 requests;
this is not an estimate of real user cache hit rate. Cache misses remain around
2.5 seconds with reranking.

The optional Redis cache is bounded to 128 entries, a 300-second TTL and 256 KiB per
payload (up to 32 MiB serialized data plus overhead). Keys include every request
constraint, exclusions, concerns, catalog/index generation, model identity, ranking
configuration and relevant code fingerprints. Readiness/model checks still run on
hits. Redis failures and malformed entries fall back to retrieval; no global Redis
configuration or unrelated keys are changed.

## Index experiment

A disposable temporary copy of the active 50k vectors was tested with exact search
and HNSW at three `ef_search` settings. Fourteen queries included filtered and broad
searches, with five repetitions and recorded query plans. Index creation took
39.09 seconds and occupied 194.4 MiB. These are vector-stage measurements, not
end-to-end hybrid retrieval timings.

| Strategy | Median of query medians | Mean overlap with exact top-50 | Minimum overlap |
|---|---:|---:|---:|
| Exact | 14.00 ms | 100% reference | 100% reference |
| HNSW, ef 40 | 1.37 ms | 93.67% | 74% |
| HNSW, ef 100 | 1.43 ms | 99.00% | 96% |
| HNSW, ef 200 | 2.02 ms | 99.67% | 98% |

**Serving retrieval remains exact.** Even ef 200 missed a neighbor on the perfume
and running-shoe cases. This temporary-table benchmark does not establish equivalent
hybrid relevance or production query plans. Candidate overlap is not human relevance.
An ANN rollout requires an agreed accuracy threshold and end-to-end validation.

## Artifacts, validation and next work

The local serving configuration points to `data/enriched_50k`, enables Redis, and
enables startup model warm-up. Reproduction commands and operational behavior are in
[scaling and performance](../docs/scaling_and_performance.md).

Final validation: **205 tests passed**, including isolated PostgreSQL integration
tests and real Redis TTL/eviction tests. There is one existing Starlette/httpx
deprecation warning. `git diff --check` passed. Live `/health` reported warm-up ready
against the expected build (5.37 seconds for this API startup). Two identical live
searches returned identical IDs: first a cache miss at 3.408 seconds, then a hit at
15.2 ms. This smoke check establishes serving integration, not an additional quality
evaluation; broad skin-care retrieval can still include a body lotion for a facial
query. Its capture is [serving verification](../data/evaluation/serving_verification.json).

Provenance:

- Sample run: `7d10261065264a7fd890`.
- Enriched run: `fe1cc2ed45832b2504bc`.
- Catalog import: `4dabfd01cb760d5c22d183f849456018142a1612495cc260a97fced5ea9ec749`.
- Search build: `741100556a74398aec06f6e2eee0a866191394d0f30fead9b55cecd72cf8411a`.

Raw local artifacts (under ignored `data/`, not bundled in a Git checkout):

- [20k captures](../data/evaluation/conversations_20k.json), [50k captures](../data/evaluation/conversations_50k.json).
- [20k summary](../data/evaluation/summary_20k.json), [50k summary](../data/evaluation/summary_50k.json).
- [20k latency](../data/evaluation/baseline_20k_latency.json), [50k retrieval](../data/evaluation/latency_50k_uncached.json), [cold reranking](../data/evaluation/latency_50k_rerank_cold.json).
- [Redis benchmark](../data/evaluation/cache_50k.json), [index benchmark and plans](../data/evaluation/index_benchmark_50k.json).
- [Sampling execution](../data/scaled/execution_report.json), [measured enrichment execution](../data/enriched_50k_measured/execution_report.json), [metadata coverage](../data/enriched_50k_measured/runs/fe1cc2ed45832b2504bc/enrichment_report.json).

Next quality work should repair rejection of explicit product clauses and recipient
extraction, then multi-product task creation/switching, before broader evaluation.
Follow with product-type precision, explanation paraphrase handling and useful
responses when ingredient/allergy evidence is unavailable. A human-reviewed set
remains deferred per the requested AI-only scope.

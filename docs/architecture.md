# Architecture and contracts

## Request processing

FastAPI `app/main.py` composes provider clients, persistence, admission controls
and monitoring. Routes validate HTTP inputs; services apply ownership, inference,
shopping policy and atomic persistence. Run one worker per conversation SQLite DB.
The [development guide](development.md) maps modules and validation commands;
[operations](operations.md) covers deployment, credentials and recovery.

SQLite stores conversations, transcripts, structured shopping state, retry records,
users and usage accounting. PostgreSQL owns authoritative catalog snapshots and
search-generation metadata. DuckDB writes immutable Parquet embedding batches;
Qdrant is the online retrieval engine. Redis is an optional bounded cache.
The sibling ETL repository normalizes source archives and publishes Parquet/JSONL.

## Conversation contracts

Create a session with `POST /conversations`, using `mode: "commerce"` and an
optional `storefront_id`. Submit messages to `/conversations/{id}/messages`.
Authenticated ownership applies to listing, messages, preferences and deletion;
administrators do not bypass ownership. Unknown and foreign IDs both return 404.
`GET /conversations/{id}/messages` returns persisted history with `after` and
`limit` pagination. `GET /conversations/{id}/preferences` exposes shopping state.

A turn proposes structured intent, validates literal evidence and constraints,
updates the selected task, retrieves eligible products when requested, and renders
a grounded response. A malformed or invalid proposal gets at most one repair
attempt; failed repair clarifies without applying rejected changes. Exact supported
commands and complete recipient answers may use a deterministic fast path. Singular
and plural recipient aliases share department mappings while retaining the user's
literal wording as evidence. Fully specified product lists with a separate dollar
budget for each item also bypass intent inference; additional qualifiers and
ambiguous shared constraints still use the planner. Preferences live in structured state,
not rolling chat summaries. Each task retains its constraints and displayed results.

Ordinary category words such as clothes, garments and outfits support broad browsing.
The planner can classify an unfamiliar literal product within a selected shopping
section. A family classification quoting the product narrows retrieval; when omitted,
search stays within the immutable section and ranks the literal product goal without
inventing a narrower category. Known product mappings remain authoritative. A season or recipient alone
cannot establish an unfamiliar product goal. Query aliases are checked before validation
and normalized only for retrieval; saved text and evidence retain the user's wording.
The clothing section accepts `cloths` as a spelling variant while keeping cleaning
cloths distinct. Equivalent clothing names reuse the same task, and seasonal preferences
remain literal soft needs rather than inferred material or performance requirements.

The planner receives bounded recent dialogue as untrusted context. Current input
and saved state take priority; reset excludes earlier dialogue. Follow-ups such as
more, cheaper, details, comparison and feedback refer to task-specific results.
Questions about displayed products need not search again. Rejected products can
be excluded from subsequent results. Multiple product clauses retain separate
budgets and concerns; uncertainty prompts clarification instead of invented filters.
A bare coordinated list can share a trailing literal concern, such as shampoo and
conditioner for dandruff. Each preference still cites actual message text; separate
clauses, prices, recipients and hard filters are never copied between tasks.

Explicit result criticism such as “these look too formal” is retained as negative
feedback, even when a planner proposes it as a positive concern. Rejected IDs stay
excluded from the task. Bounded feedback examples retain displayed product titles
after pagination so follow-ups can refer to liked options. They are untrusted soft
style context, never product facts or hard requirements, and remain task-scoped.
Evidence-ranked results conservatively collapse repeated titles and very similar
same-brand listings while retaining distinct model numbers. Partial-match notices
remain visible when only some saved concerns have listing support.
When candidates cover different numbers of saved concerns, return the strongest
coverage group rather than padding it with weaker matches merely to reach five.
Distinct recognized topics in one phrase, such as “comfy for office,” require
evidence for both topics before counting as a supported concern. Unrequested
holiday/costume formats and sports uniforms rank below ordinary options.

With `COMMERCE_FAST_PATH=1`, validated recommendation lists and required clarification
questions are returned directly, including semantic refinements and multi-product results. An explicit request
for an explanation keeps the reviewed reply path. This avoids optional model writing
and review calls after the backend has already prepared a complete grounded reply.
Set `COMMERCE_FAST_PATH=0` to retain the reviewed path for these results.
Narrow fragrance questions also use a source-backed direct answer: an explicit
fragrance-free claim is reported as a listing claim; a scent label alone never
establishes whether added fragrance ingredients are present. Broader ingredient
questions keep the reviewed path, without a new search.

Other natural replies use evidence-cited generation and a separate model review. Product
identities/prices are rendered by code. Rejected or oversized generated text falls
back to a validated deterministic reply. Generation cannot modify filters or call
tools. This review is not a guarantee of factual accuracy or recommendation quality.
The optional older evidence-selection summary applies when natural mode is disabled.

With `Accept: text/event-stream`, the API emits progress, commerce metadata and
reply deltas. Only validated backend replies or reviewed generated content are sent
as the answer. The terminal `done`
event follows atomic persistence of both messages, shopping state and replay data.
Disconnects do not imply a failed commit: retry with the same idempotency key.
Chat-mode context contains a system prompt, rolling summary and up to six recent
turns bounded by UTF-8 bytes; original transcripts remain stored separately.

## Storefront isolation

Beauty, clothing, footwear and fashion sessions have separate state. The frontend
keeps section-specific cookies and the backend enforces the recorded storefront.
A cross-section request produces a handoff instead of silently changing the session.
Mixed requests cannot pull out-of-section products into the current storefront.
Storefront isolation and conversation ownership do not provide private tenant
catalog isolation; catalog/Qdrant/cache tenant scope is separate future work.

## Catalog and enrichment

Imports verify completed ETL manifests, selected source records and schema before
publishing immutable catalog snapshots. Applied SQL migrations are checksummed;
add migrations instead of changing previously applied files. Catalog staging leaves
the active serving generation unchanged until a compatible index is verified.

Enrichment keeps selected product IDs, order, prices and observations. It adds
source-backed attributes and concern evidence to a separate snapshot; it does not
expand the sample or infer missing stock, ingredients or variant availability.
Marketing mentions remain listing claims, with source fields and excerpts retained.
The parent-product dataset does not establish current prices, inventory, medical
outcomes or complete purchasable variant coverage.

## Search and reranking

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

Reranking uses only already eligible candidates; it cannot relax filters or add
products. `PRODUCT_RERANK_CANDIDATES` sets the default pool (20–50). Validated
indices/scores map back to original IDs, provenance and listing evidence. Ties
retain deterministic ordering. API metadata identifies the actual model/device.
Voyage can fall back to a small local CPU model; see [providers](operations.md#model-providers).

## Embedding lifecycle and cache

A generation pins the catalog, input producer/version, model identity, dimensions
and vector files. Reuse requires the same selected-input hash and complete embedding
contract. Changed text/model contracts require new inference; metadata-only changes
can reuse compatible vectors while hydrating current facts. Each generation is a
complete immutable snapshot, even when most vectors were reused.

The builder verifies Qdrant before atomically publishing catalog/search pointers.
Completed batches survive failure and can resume the same build. Concurrent catalog
changes prevent publication. Missing products disappear from the next active snapshot.
Switching embedding providers requires a new compatible generation; never mix query
vectors from a new provider with an old index. Hosted identity is a contract hash,
not proof that provider weights have stayed unchanged.

Cache keys include generation, collection, retrieval mode, model identity, constraints
and ranking-code identity. Queries validate the active collection before serving a
cache hit. Redis failures do not disable search. Qdrant failures do: PostgreSQL is
not an online fallback. Retain PostgreSQL metadata and Parquet snapshots together
so Qdrant can be rebuilt without paid inference.

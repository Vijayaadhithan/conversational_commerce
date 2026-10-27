# Architecture

## Request processing

`app/main.py` composes the FastAPI application, provider clients, persistence,
admission controls, and lifecycle monitoring. Route modules validate HTTP input
and delegate to services. `chat_service.py` handles inference and bounded context;
`conversation_service.py` checks ownership and dispatches chat versus commerce;
`commerce_service.py` coordinates validated shopping turns and atomic persistence.
`commerce_models.py` owns schemas, `commerce_taxonomy.py` owns the literal product
vocabulary, and `commerce_prompts.py` owns planner instructions and examples.
See [development standards](development.md) for module responsibilities.

Each conversation has an owner, UUID, and SQLite transcript. User messages,
assistant responses, state changes, and idempotency results commit together.
Per-conversation locks serialize changes. An OS lock limits serving to one worker
per conversation database. Authentication, request limits, and bounded model
admission apply to local and hosted inference. See [operations](operations.md).

Chat context contains the system prompt, optional rolling summary, recent turns,
and current message. At most six completed turns remain in the recent window;
the UTF-8 budget may reduce that further. Original transcripts remain available.
Shopping preferences use structured state rather than transcript summaries.

## Shopping workflow

The model proposes evidence-backed intent. Deterministic validation checks literal
quotes, constraints, prices, and task scope before applying changes. Separate
modules handle task selection, preference updates, discovery, and grounded replies.
Each product task retains its own constraints and recommendation snapshots.
Malformed intent and invalid proposals share one bounded repair attempt, then clarify
without applying rejected changes. Business-scope classification shares the planner
call. Optional summaries select only supplied evidence IDs and stream validated
highlights; `done` is sent only after atomic persistence. See
[conversation contracts](conversations.md).

## Inference providers

Chat supports Ollama, Google AI Studio, GroqCloud, and OpenRouter. Embeddings
support Ollama and Google AI Studio. Reranking supports the local Mixedbread
model and Voyage. Components are configured independently. See
[provider configuration](model_providers.md) for credentials and migration.

Local reranking loads pinned files without remote model code. Hosted calls use
fixed HTTPS endpoints, bounded timeouts, and sanitized errors. Provider changes
do not bypass authentication, evidence validation, or mandatory search filters.

## Retrieval and storage

The separate ETL repository uses DuckDB to normalize and validate source archives,
then publishes versioned Parquet and JSONL. PostgreSQL stores authoritative product
snapshots, hard-filter projections, and generation metadata. Backend DuckDB writes
immutable, checksummed Parquet embedding batches; PostgreSQL stores only their
references. These snapshots support reuse, verification, and Qdrant rebuilds.
Conversation SQLite is independent live state.

Qdrant is the only online candidate-retrieval engine: native BM25 and dense cosine
search return candidates, which the backend fuses with RRF. Mandatory SQL eligibility
and storefront rules restrict both branches before retrieval. Product facts are
hydrated from the same pinned PostgreSQL snapshot. Shared listing evidence and
reranking preserve the response contract. See [search](search.md).

The builder finishes and verifies the Qdrant collection before publishing catalog
and search pointers. Queries require that exact ready collection, including before
serving cached results. Qdrant errors fail explicitly; PostgreSQL is not an online
fallback. Redis caches are bounded and include generation, collection, retrieval
mode, model identity, request constraints, and ranking-code identity.

See [catalog storage](catalog.md), [embedding lifecycle](embedding_lifecycle.md),
[operations and retention](operations.md), and [reranking](product_reranking.md).

## Boundaries

Catalog snapshots do not establish current stock, prices, medical outcomes, or
complete ingredient and variant coverage. Distributed admission, identity
federation, and approximate-search quality validation remain separate work.

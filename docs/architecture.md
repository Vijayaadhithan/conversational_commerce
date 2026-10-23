# Architecture

## Request processing

`app/main.py` composes the FastAPI application, provider clients, persistence,
admission controls, and lifecycle monitoring. Route modules validate HTTP input
and delegate to services. `chat_service.py` handles inference and bounded context;
`conversation_service.py` coordinates validated turns and atomic persistence.
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
Invalid proposals clarify without applying rejected changes. See
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

PostgreSQL stores catalog snapshots and 768-dimensional pgvector embeddings.
Catalog imports retain source provenance, unknown attributes, and historical USD
prices. Search generations publish atomically after complete validation.
Incremental builds reuse compatible vectors; inactive generations support rollback.
See [catalog storage](catalog.md) and [embedding lifecycle](embedding_lifecycle.md).

Retrieval combines English full-text search and exact cosine distance using RRF.
Both paths apply identical mandatory SQL filters. The selected reranker scores
eligible candidates only. Redis optionally caches bounded, versioned results.
Cache identity includes the search generation, request constraints, reranker
configuration, and relevant code. See [search](search.md) and
[reranking](product_reranking.md).

## Boundaries

Catalog snapshots do not establish current stock, prices, medical outcomes, or
complete ingredient and variant coverage. Distributed admission, identity
federation, and approximate-search quality validation remain separate work.
Historical performance measurements are documented in
[scaling and performance](scaling_and_performance.md).

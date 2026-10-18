# Conversational product recommendations

Create a conversation with `mode: "commerce"`. The existing default `chat` mode
continues to provide general local conversation. Both use the configured Ollama
chat model; this feature does not download or replace models.

```bash
# First terminal
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Second terminal
.venv/bin/python scripts/chat.py --commerce
```

Try this sequence:

1. `I have dry skin` — save the concern and ask which product type is wanted.
2. `Show me a moisturizer under $30` — retrieve products for that concern,
   applying a strict historical USD maximum of $29.99.
3. `Under $20 instead` — change the maximum to $19.99 while keeping the goal,
   concern, brand, and other requirements.
4. `Compare the first two` — compare the saved product snapshots and prices.
5. `More options` — exclude products already shown in this shopping task.
6. `Make it fragrance-free` — require an explicit listing claim. An unknown
   attribute is excluded, even if that means no results.
7. `Any brand is fine` — explicitly clear positive and negative brand filters.
8. `Now I also need a shirt` — create a separate task and ask for recipient
   context before apparel/footwear search.
9. `Back to the moisturizer` — restore that task's saved requirements.

`/preferences` displays the saved state. `/history` displays the transcript.
`/new` creates another session in the same mode; `Start over` explicitly clears
the shopping tasks while keeping the transcript. Resume after restart with
`scripts/chat.py --session CONVERSATION_ID`.

## Request and response

```bash
curl -sS http://127.0.0.1:8000/conversations \
  -H 'Content-Type: application/json' \
  -d '{"title":"Skin care","mode":"commerce"}'

curl -sS http://127.0.0.1:8000/conversations/CONVERSATION_ID/messages \
  -H 'Content-Type: application/json' \
  -d '{"message":"A moisturizer for dry skin under $30"}'

curl -sS http://127.0.0.1:8000/conversations/CONVERSATION_ID/preferences
```

The message response retains `reply`, `message_id`, model, and usage fields. Its
`commerce` object contains:

| Field | Meaning |
| --- | --- |
| `intent` | Validated action and evidence-backed preference changes |
| `action` | Clarification, recommendation, comparison, details, preferences, or small talk |
| `preferences` | Active task, each task's goal/concern/recipient, filters, change sources, pending question, seen count |
| `state_version` | Monotonic version committed with the completed turn |
| `products` | Catalog records with original product IDs, listing provenance, display positions, and `why_recommended` |
| `search` | Pinned catalog/build, actual applied filters, retrieval/reranker metadata, or null when no retrieval ran |

Assistant messages returned by the paginated history endpoint contain the same
`commerce` metadata, so clients can restore product cards and comparisons after
restart. `GET /conversations/{id}` additionally exposes the full saved state,
including ordered recommendation snapshots and seen IDs.

## Processing and persistence

The local model first proposes structured intent using Ollama's JSON Schema
format. The prompt receives saved task summaries, active constraints, recent
product titles, a pending clarification, and the latest user message. It does
not use a lossy transcript summary as authoritative preference storage.

Code checks that proposed values and quotes come from the current message,
requires explicit constraint removals, and validates price arithmetic and the
filter schema. Omitted preferences remain unchanged. Conflicting price bounds
produce clarification while preserving the previous valid range. Unsupported
model output fails with 502 and saves no partial turn.
Unchanged restatements are discarded while retaining their original sources.
A small product-name lexicon can recover an explicitly stated goal omitted by
the model; it never infers a product type from a concern alone.

Discovery uses the existing pipeline: mandatory PostgreSQL filters, keyword
and vector retrieval, RRF, then the local reranker. The final text is assembled
from retrieved records, historical prices, explicit attributes, and cited
listing excerpts. It does not generate additional product facts with the LLM.
An explanation identifies relevant evidence; it does not prove that a product
will resolve a concern.

Simple commands and numbered details/comparisons skip model inference.
Comparisons use the last successful recommendation list for that task, with
the original display positions and search provenance. An empty search does not
erase those previous references or relax filters. Those older snapshots are
for inspection, not a new claim that they match newly changed requirements.
`Cheaper` uses the lowest price in the referenced list, subtracts one cent, and
preserves any stricter existing maximum. `Another brand` excludes known brands
in the displayed list; unknown brands do not satisfy that exclusion.

SQLite stores transcripts, structured state, and per-turn product metadata in
one transaction after successful extraction and search. Startup applies an
additive migration to older chat databases. Optimistic version checks reject
stale writes with 409; the API generation lock serializes local model work.
Continue using one server worker. Failed catalog/model/reranker calls do not
save a half-turn. There is no request-id deduplication; an ambiguous network
failure should be checked in history before retrying.

## Current boundaries

- Beauty and fashion use the normalized 20,000-product sample and controlled
  product families. This is a first implementation, not unrestricted intent
  understanding or a comprehensive relevance evaluation.
- Each conversation supports five separate shopping tasks; each task remembers
  up to 100 shown IDs and ten excluded brands. At a limit, the API asks for an
  explicit reset rather than silently dropping exclusions.
- Text attributes match explicit metadata exactly, ignoring case. Missing size,
  recipient, ingredients, or other attributes remain unknown. Parent listings
  do not establish variant availability or fit.
- Prices are historical USD. Non-USD budgets, current stock/delivery, and
  verified allergy or medical suitability require clarification. Foreign
  amounts are never converted or silently interpreted as USD. Continuing
  without an unsupported requirement requires an explicit removal or a clear
  affirmative response to the removal question.
- Replies are grounded templates with source fields; richer natural language
  answer generation, cart/checkout, live retailer feeds, multi-user auth, and
  Redis caching are subsequent work.

## Validation

```bash
CATALOG_TEST_ADMIN_URL=postgresql:///postgres .venv/bin/python -m pytest -q
.venv/bin/python scripts/check_commerce.py
```

The first command uses isolated PostgreSQL scratch databases. The second uses
the installed local chat/embedding/reranker models and active catalog, runs one
scripted conversation plus restart, and writes an ignored local report to
`data/search/commerce_report.json`. Its temporary SQLite database is removed
automatically. This checks continuity, provenance, and constraints; it does
not provide human-judged recommendation accuracy metrics.

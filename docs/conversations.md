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

1. `I have dry skin` — save the concern and ask about skin type/budget, with an
   option to browse skin-care listings without choosing a specific product type.
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

You can also ask for `shampoo under $15 and moisturizer under $25`. The reply
contains separate product groups, with independently saved budgets and listing
references. `Shampoo under $10 instead` updates only the shampoo task.
After multiple groups, a bare `compare the first two` asks which task you mean;
`details about the first in task 2` selects a specific group's first product.

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
| `intent_attempts` | One extraction attempt, or two when a rejected proposal was corrected and revalidated |
| `products` | Catalog records with original product IDs, listing provenance, display positions, and `why_recommended` |
| `search` | Pinned catalog/build, actual applied filters, retrieval/reranker metadata, or null when no retrieval ran |
| `groups` | Present for `action: multi`: each task's reply, products, action, query, task ID, and search metadata |
| `validation_recovered` | True when rejected proposals resulted in clarification with saved requirements unchanged |

Assistant messages returned by the paginated history endpoint contain the same
`commerce` metadata, so clients can restore product cards and comparisons after
restart. `GET /conversations/{id}` additionally exposes the full saved state,
including ordered recommendation snapshots and seen IDs.

## Processing and persistence

The local model first proposes structured intent using Ollama's JSON Schema
format. The prompt receives saved task summaries, active constraints, recent
product titles, a pending clarification, and the latest user message. It does
not use a lossy transcript summary as authoritative preference storage.
Optional examples use only the space remaining after the full state, current
message, schema, and any validation feedback have been included.
The same model call also sets `intent.recommendation_requested`: a semantic
decision about whether the user wants products now, using the message, saved
concerns, and pending clarification. Requests such as `give me soem products
with the above conditions` or `what would you pick for me?` start discovery
without a special command. Quoted commands, discussion, and refusals do not.
This does not require a separate inference call or relax mandatory filters.

Code checks that proposed values and quotes come from the current message,
requires explicit constraint removals, and validates price arithmetic and the
filter schema. Omitted preferences remain unchanged. Conflicting price bounds
produce clarification while preserving the previous valid range. Unsupported
model preference proposals receive one correction attempt with validation
feedback. A second invalid preference proposal produces a normal clarification
reply, with none of the rejected preference changes applied. The user message
and clarification are saved together so the conversation can continue.
Model connection, malformed JSON, catalog, and reranker failures are not retried
by this correction path. Usage counts include both completed extraction calls.
Unchanged restatements are discarded while retaining their original sources.
A small product-name lexicon can recover an explicitly stated goal omitted by
the model; it never infers a product type from a concern alone.

Multiple `need` changes in a turn accumulate as separate `needs` entries with
their original evidence. Additional concerns preserve earlier ones; exact
repetitions and redundant wrapping words are ignored. When a model paraphrases
a concern backed by a literal multiword quote, the quote is saved as the concern
instead of the model's interpretation. Older sessions with one `need` string are upgraded
when continued. Scalar filters such as budget and brand still reject
conflicting duplicate changes. Removing one named concern preserves the others;
an ambiguous removal asks for clarification rather than clearing all concerns.

Facial/skin concerns can prompt for optional skin context and USD budget. A skin-profile
answer or a positive recommendation-intent decision can start broad `skin_care`
discovery. Its source is marked `concern_guided_discovery`, not a user-selected
serum, cleanser, or moisturizer. Skin context is soft relevance text unless a
listing attribute is explicitly required. Listing matches do not establish that
a product will lighten skin, reduce pimples, or provide a medical outcome.

Vague preferences such as `something that is kess chemincal` retain their
original wording and prior concerns. The assistant asks which ingredient or
property the user means; it does not infer fragrance-free, ingredient exclusions,
or safety from that phrase. `Show options` can continue broad discovery.

Explicit multi-product clauses create or refine separate tasks, including two
products from the same family. Extraction supplies complete verbatim clauses;
code checks clause coverage and ensures each change's evidence belongs to that
product. Shared requirements such as `both fragrance-free` or a combined budget
ask for per-product scope before searching. Task limits reject the entire batch
instead of dropping a product. Each task retains its own pending clarification,
filters, shown products, and history. The top-level product list contains task IDs
for grouped replies, and clients should display each group's own numbered list.

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
stale writes with 409; per-conversation locks serialize state mutations and bounded
admission controls model work. Continue using one server worker. Failed
catalog/model/reranker calls do not save a half-turn. Reuse the original
`Idempotency-Key` after an ambiguous network failure; completed retries return
the saved response. Bearer tokens scope access to the conversation owner.
See [API operations](operations.md) for the full retry and authentication contract.

## Current boundaries

- Beauty and fashion use the enriched 50,000-product catalog and controlled
  product families. This is a first implementation, not unrestricted intent
  understanding or a comprehensive relevance evaluation.
- Each conversation supports five separate shopping tasks; each task remembers
  up to 20 concerns, 100 shown IDs and ten excluded brands. At a limit, the API asks for an
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
  answer generation, cart/checkout and live retailer feeds remain subsequent work.

## Validation

```bash
CATALOG_TEST_ADMIN_URL=postgresql:///postgres .venv/bin/python -m pytest -q
.venv/bin/python scripts/check_commerce.py
.venv/bin/python scripts/check_concern_flow.py
```

The first command uses isolated PostgreSQL scratch databases. The second uses
the installed local chat/embedding/reranker models and active catalog, runs one
scripted conversation plus restart, and writes an ignored local report to
`data/search/commerce_report.json`. Its temporary SQLite database is removed
automatically. This checks continuity, provenance, and constraints; it does
not provide human-judged recommendation accuracy metrics.
The concern-flow check replays typo-containing facial concerns and vague
preferences, repeated follow-ups, a skin-profile answer, catalog retrieval,
multi-product requests, scoped follow-ups, comparison, continuation
from the older saved-session format, and restart
using another temporary conversation database. Its report is
`data/search/concern_flow_report.json`.

`.venv/bin/python scripts/check_recommendation_intent.py` checks the reported
typo conversation, four natural browsing requests from the same saved context,
two non-browsing turns, and restart with real local models and catalog data.
It writes `data/search/recommendation_intent_report.json` using temporary
conversation storage, without modifying existing sessions.

# Conversation-driven shopping

The planner interprets informal shopping requests in the selected storefront. An
active browse continues when the user adds a preference or rejects results; budget,
recipient and product subtype are optional unless the user makes them requirements.
Explicit pauses and product inspection do not start another search. Cross-section
requests still use the existing visible handoff without transferring constraints.

`IntentPlan.interpretation` is an optional `{query, evidence}` retrieval hypothesis.
Its evidence must quote the current message. The query can correct typos and express
style alternatives, but it never sets filters, changes the literal product identity
gate, or establishes product facts. The saved interpretation is reused only while
the goal, concerns, recipient and filters match the brief it described. Actual user
preferences keep their source wording. Single-word spelling clarifications replace
redundant concerns and retain the original in `sources.resolved_concerns`.

A weak semantic search can make one additional search using the literal brief with
identical filters, identity gate and exclusions. The response records both attempts
and the chosen attempt in `search.retrieval_recovery`. No results are merged across
catalog snapshots. A tie keeps the original search. Missing evidence remains a
partial match; linguistic aliases are listing-text matches, not predicted outcomes.
Compound concerns require all components before receiving positive evidence support.
Unrequested costume and specialist workwear listings receive a visible ranking caution.
When supported matches exist, return those rather than padding the list with weaker
matches. If none exist, retain clearly explained partial matches.

Open-ended results with a semantic interpretation use the existing evidence-cited
writer and reviewer. Exact navigation commands and simple results retain fast
code-rendered replies. `COMMERCE_ANSWER_TIMEOUT_SECONDS` bounds the combined writing
and review work (default 8 seconds); failure returns the already prepared grounded
reply and products. Product IDs, prices, filters, evidence and critical limitations
remain code-owned. Streaming still emits provisional products before writing.

## Evaluation

Run the state, contract and ranking regressions without model weights:

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check app scripts tests
.venv/bin/ruff format --check app scripts tests
```

Run the live-dependency conversations against an isolated conversation database:

```sh
.venv/bin/python scripts/evaluate_conversations.py \
  --cases evals/conversations_browsing.json \
  --output data/evaluation/browsing_capture.json
```

The capture includes per-turn scenario checks, first-products turn, consecutive
clarifications, returned rejected products, response routes and stage timings.
These mechanical checks do not establish relevance or naturalness. Inspect actual
products, listing support and complete replies before promoting a candidate. Existing
catalog/search integration tests require their isolated PostgreSQL/Qdrant test URLs.

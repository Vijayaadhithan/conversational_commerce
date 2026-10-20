# Conversation repairs and follow-up evaluation

19 October 2026. AI-reviewed synthetic conversations; no human review.

Follow-up: [20 October recommendation quality and metadata improvements](recommendation_quality_2026-10-20.md). The measurements below describe the earlier catalog and code.

This follows the [50,000-product evaluation](commerce_evaluation_2026-10-19.md).
The catalog and embedding generation are unchanged. This change repairs conversation
interpretation, task continuity, listing precision and explanations.

## Results

| Evaluation | Successful | Partial | Failed |
|---|---:|---:|---:|
| Original 12 cases before repairs | 3 | 2 | 7 |
| Original 12 cases after repairs | 10 | 2 | 0 |
| Six additional cases, each run twice | 8 | 4 | 0 |
| Combined final outcomes | 18 | 6 | 0 |

All 57 final reviewed turns returned HTTP 200. **248 tests passed**, including
isolated PostgreSQL and Redis integration tests; one existing Starlette/httpx
deprecation warning remains. `git diff --check` passed. The local API was restarted
with these changes and reported warm-up ready against the unchanged 50k build.
A live filtered search followed by a cache hit returned identical product IDs.
That smoke check measured 5.26 seconds then 14.1 ms while evaluation work was also
running; these are integration observations, not a new performance benchmark.

The original partial outcomes are the undefined “less chemical” request and the
women's running-shoe catalog gap. In the additional cases, the shoe gap persists,
and the clarified fragrance-free request returns three listings with brightening
wording plus two without that evidence. Both repeated runs produced identical
action sequences and product ID lists. Budget isolation, task restoration,
unseen-option exclusion, explicit preference resolution and no-match recovery were
also checked directly against the captured state.

Across the 100 reviewed product positions, 79 were strong matches, 20 partial and
one poor. The poor fit is a chemical peel during broad browsing with the unresolved
“less chemical” preference. Its condition is explicitly unverified; it still
receives a poor fit grade. Other partial product grades include extra accessories,
child-sized or personalized items, weak keyword evidence and conflicting listing
wording. **Zero failed conversations does not mean every recommended product is a
strong match.** There were zero violations of extracted structured filters across
207 card occurrences; this check does not prove that all user requirements were
extracted or that listing claims are accurate.

The original-case product denominator grew from 25 to 50 because previously rejected
requests now return products. Its strong-match fraction changed from 68% to 78%,
but this is not a controlled comparison on an unchanged set of returned products.

## What changed

| Priority | Implemented behavior |
|---|---|
| Clear product requests | Accept a validated single request whether the model emits top-level changes or one request wrapper. Recognize handbag and razor-blade phrases. Recover omitted literal product goals. |
| Recipient and context | Map explicitly stated men/women to department filters; recognize spelling/punctuation equivalents such as `Women's` and `womens`. Derive taxonomy codes from named products instead of rejecting a shirt because the model calls its domain `apparel` or `beauty`. |
| Multiple products | Keep separate product clauses and budgets, tolerate an omitted request verb in model evidence, handle compound names, and route shortened references back to saved goals without losing modifiers. Ambiguous shared requirements still require clarification. |
| No matches and recovery | Explain the named product and budget that had no matches, preserve constraints, and refresh results on explicit budget changes. Earlier product references remain available but are labeled when current constraints differ. |
| Vague preferences and evidence limits | Preserve unclear wording and ask what it means; explicit clarification such as “I mean fragrance-free” resolves it with an audit trail. Allergy questions receive an explanation of evidence limits instead of a generic preference question. |
| Product precision and explanations | Apply supported product-type checks before both retrieval paths. Exclude known containers/accessories and products for a different use. Match coordinated wording such as “dry & normal skin” and “curly and wavy hair.” |
| Broader validation | Add six unfamiliar conversations covering request typos, revisions, more options, five-turn task switching, clarification and no-match recovery. Run each twice alongside the original twelve cases. |

The model still interprets general conversational intent. Narrow direct commands
handle explicit task returns and budget changes; questions and deferrals continue
through the semantic planner. Evidence validation, atomic state updates, strict
budgets, separate task constraints and the five-task limit remain enforced.

## Product evidence and catalog limitations

The new optional `ProductSearchRequest.product_goal` carries the explicit goal
separately from soft concerns. Conversation searches supply it automatically.
Parameterized SQL applies the corresponding identity checks to the shared eligible
set before keyword search, exact vector search and reranking. Search metadata records
the goal, and the existing Redis key includes it. Direct API clients can supply it;
omitting it preserves their previous retrieval behavior.

The checks require listing wording, not verified product performance. Examples found
during this work included empty lipstick tubes, lipstick brooches, shoe charms,
handbag inserts and scraping blades. Razor-blade recommendations now also require
explicit shaving-related wording. Facial moisturizer requests require facial-use
wording; running-shoe requests require running-shoe identity. Unknown product types
retain the existing retrieval path. These conservative rules can reduce recall and
do not replace a fully curated catalog taxonomy.

The active sample does not provide qualifying women's running-shoe listings under
the evaluated constraints. The system now explains no matches without substituting
children's shoes, charms or unrelated footwear. Listing gaps, historical prices,
unknown variants and incomplete ingredient data remain unchanged.

“Less chemical” remains unresolved unless the user defines a concrete property.
Browsing may show related listings, explicitly marked as unverified against that
preference. It is not a guarantee of ingredient composition or suitability.

## Method and artifacts

The final validation set contains 24 conversations and 57 turns: the original 12
conversations/23 turns plus two passes of six new conversations/17 turns per pass.
All use real local `ministral-3:8b`, EmbeddingGemma and the MPS reranker against the
same 50,000-product build. Chat storage is temporary and isolated from user chats.
The new cases were not added as planner examples. They were inspected during this
repair cycle, so they are regression coverage, not a held-out generalization test.

AI review uses the previous 0/1/2 rubric. Conversation outcome covers all turns;
product grades cover the final nonempty recommendation list. HTTP success and
structured-filter compliance are distinct from task success and product relevance.
No human satisfaction, corpus recall, medical efficacy or production-load claim is
made. Repeated temperature-zero runs do not establish reliability across all wording
or future model versions.

Reproduction:

```sh
.venv/bin/python scripts/evaluate_conversations.py --output data/evaluation/original_recheck.json
.venv/bin/python scripts/evaluate_conversations.py --cases evals/conversations_extended.json --repetitions 2 --output data/evaluation/extended_recheck.json
CATALOG_TEST_ADMIN_URL=postgresql:///postgres CATALOG_TEST_REDIS_URL=redis://127.0.0.1:6379/0 .venv/bin/python -m pytest -q
```

Fixtures: [original](../evals/conversations.json), [extended](../evals/conversations_extended.json).
Raw captures live under ignored `data/evaluation/`; reviewer labels and this report
are tracked separately. Captures record the planner prompt hash, case-set hash and
application module hashes for provenance.

Six affected conversations (15 turns) were rerun after the final blade, handbag and
clarification changes. The assembled final capture uses those complete reruns and
identifies the source capture for each case. Both raw captures are preserved; the
57-turn figure describes the final reviewed set, not total development attempts.

- [AI review labels](../evals/review_conversation_fixes_50k.json)
- [Combined reviewed capture](../data/evaluation/conversations_final_review_50k.json)
- [Initial combined validation capture](../data/evaluation/conversations_validation_fixed_50k.json)
- [Six-case superseding capture](../data/evaluation/conversations_precision_recheck_50k.json)
- [Combined summary](../data/evaluation/summary_conversation_fixes_50k.json), [original-case summary](../data/evaluation/summary_original_fixed_50k.json), [extended summary](../data/evaluation/summary_extended_fixed_50k.json)
- [State and repeatability checks](../data/evaluation/conversation_behavior_checks.json)
- [Live API and cache verification](../data/evaluation/serving_conversation_fix_verification.json)

Remaining work is broader catalog coverage, cleaner product identity metadata and
more diverse evaluation beyond these repaired regression cases. Human review remains
deferred. Unclear preferences still require a user definition; missing source
evidence remains unknown rather than an invented match.

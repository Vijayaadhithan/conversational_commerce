# Backend cleanup and remaining gaps — 2026-10-24

## Scope and evidence

Audited runtime modules, Python entry points, tests, documentation, and CI before
removal. Ruff and an AST reference scan identified no whole backend module that
could be safely classified as dead. Framework-registered routes, supported
provider adapters, operational scripts, and public compatibility exports remain
intentional. Static reference scans are candidate finders, not reachability proofs.

The checkout already contained uncommitted storefront and streaming changes.
This cleanup preserves that work and does not publish or commit it. Frontend,
credentials, catalog/index generations, model files, and the existing README
changes were not modified by this cleanup. No application servers or live model
inference were started.

## Changes completed

- Removed the unused duplicate `ATTRIBUTES` list from shopping models. Search
  attribute validation remains owned by `AttributeName` in `search_models.py`;
  preference-change fields retain their explicit schema.
- Removed redundant lint suppressions and unused loop bindings. Attribute
  evidence traversal now reads values directly instead of unused key/value pairs.
- Separated schema definitions (`commerce_models.py`), literal product vocabulary
  (`commerce_taxonomy.py`), and planner prompts/context/examples
  (`commerce_prompts.py`). Both planner prompt SHA-256 values are unchanged.
- Kept established `app.commerce`, `app.main`, and model-module prompt imports
  compatible. Runtime consumers use the owning modules.
- Moved commerce orchestration into `CommerceTurnService`. Conversation dispatch
  still checks ownership first; shopping state and replay responses still commit
  atomically. Search callbacks, progress, retries, and storefront boundaries retain
  their contracts. Service/callback boundaries now carry explicit annotations.
- Centralized token accounting in `inference_usage.py`. Missing, negative,
  fractional, or boolean counts remain unknown rather than becoming misleading
  totals. Valid counts are aggregated across planner attempts.
- Centralized restoration of shopping state using the immutable conversation
  storefront. The preferences endpoint now returns that scope before the first
  message, without writing synthetic tasks.
- Enabled CI lint rules for unused loop bindings and redundant suppressions.
  Development documentation now accurately distinguishes lint checks from a
  whole-program dead-code audit.

## Evaluation corrections

- Review validation now raises explicit errors instead of relying on assertions
  that disappear with `python -O`.
- Duplicate or mismatched case IDs, invalid grades, and invented product grade
  IDs are rejected. Human/AI review attribution is preserved.
- Structured constraint reporting now checks the originating conversation's
  storefront as well as returned filters. A wrongly scoped product cannot pass
  merely because its returned filters name the wrong section too.
- Missing commerce responses report scope checks as unassessed (`null`), not
  successful empty results.
- The report explicitly states that product grades currently cover the final
  recommendation turn per conversation. This is not a whole-journey relevance
  score. Evaluation captures now fingerprint the extracted prompt/taxonomy files.

## Verification

```bash
CATALOG_TEST_ADMIN_URL=postgresql:///postgres \
CATALOG_TEST_REDIS_URL=redis://127.0.0.1:6379/0 make check
```

Result: Ruff and format checks passed; **599 tests passed, no skips**. PostgreSQL
uses a randomly named scratch database; Redis uses a unique test prefix. Provider
inference is mocked. One existing FastAPI/Starlette test-client deprecation
warning remains. Prompt byte checks and public compatibility imports passed.

This is source cleanup and regression verification, not evidence of live model
relevance or a production-readiness certification.

## Gaps to carry into the next backend phase

1. **Business-scope enforcement.** Add an explicit off-topic/unsupported decision
   before retrieval and prevent the demo principal from opening unrestricted
   `mode="chat"` sessions. Current storefront filtering does not establish this.
2. **Planner recovery.** Schema/transport failures in `shopping_plan` currently
   leave through HTTP errors before the semantic validation repair loop. Design
   bounded recovery separately for malformed output, rejected evidence, and
   provider unavailability; never apply a rejected preference patch.
3. **Grounded answer generation.** The proposed second LLM explanation call,
   evidence-constrained output contract, timeout, and template fallback are not
   implemented by this cleanup. Preserve product IDs, amounts, source evidence,
   and unknown fields independently of generated prose.
4. **Complete-journey evaluation.** The 252 prepared cases have not been run live.
   The current capture runner remains in one conversation per case: it records
   offered handoffs but does not execute acceptance into another storefront.
   Add destination-session journeys, resumed source sessions, unrelated/mixed
   requests, and judgments of every relevant turn, not only the final product set.
5. **Measurement and release controls.** Define per-stage latency/usage metrics,
   routing false-rejection checks, constraint violations, evidence faithfulness,
   and human product relevance. Version prompt/model configuration in captures
   and compare baseline versus changes before claiming improvement.
6. **Production preparation.** Runtime dependency specifications are mostly ranges;
   reproducible dependency locking remains separate work. The service deliberately
   supports one worker per conversation database, and transcript/replay retention
   is not automated. Define backup/restore verification and retention before a
   production rollout. These limitations do not require deleting current demo data.

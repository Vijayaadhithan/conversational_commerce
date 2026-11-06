# Development standards

## Checks

Install `requirements-dev.txt`, then run `make check`. Run `make format` before
committing. Ruff configuration is versioned in `pyproject.toml`; editor defaults
are in `.editorconfig`. Formatting uses four spaces and a 100-character target.
Long SQL, regular expressions, and prompt strings retain their exact contents.

CI checks formatting, imports, undefined names, unused imports/locals/loop bindings,
and redundant lint suppressions, plus tests on Python
3.11 and 3.14. PostgreSQL, Qdrant, and Redis run as isolated test services.
Hosted inference tests use mock transports and require no provider credentials.

For local integration tests, set `CATALOG_TEST_ADMIN_URL` and
`CATALOG_TEST_REDIS_URL` and `QDRANT_TEST_URL`. Catalog/search integration tests
require PostgreSQL and Qdrant. PostgreSQL tests create and drop a randomly named scratch
database; Qdrant uses random test collection prefixes and Redis uses unique key prefixes. Without these settings, integration
tests are skipped. Use test services, never production credentials.

## Code organization

| Responsibility | Modules |
| --- | --- |
| Application composition and lifecycle | `main.py`, `config.py` |
| HTTP contracts and access checks | `api_models.py`, `api_access.py`, `routes_*.py` |
| Conversation persistence | `conversation_store.py`, `api_security.py` |
| Provider calls and prompt budgets | `chat_service.py` |
| Conversation dispatch and commerce orchestration | `conversation_service.py`, `commerce_service.py` |
| Shopping schemas, literal vocabulary, planner prompts | `commerce_models.py`, `commerce_taxonomy.py`, `commerce_prompts.py` |
| Storefront routing and boundaries | `storefronts.py`, `commerce_storefront.py` |
| Provider usage accounting | `inference_usage.py` |
| Shopping state transitions | `commerce_flow.py`, `commerce_single_turn.py`, `commerce_task_*.py` |
| Display references and literal product reactions | `commerce_feedback.py` |
| Natural replies, evidence packets and semantic review | `commerce_answer.py`, `commerce_answer_context.py` |
| Fast response routing and per-turn timings | `commerce_performance.py` |
| Preference validation and retrieval intent | `commerce_preferences.py`, `commerce_intent.py`, `commerce_requests.py` |
| Retrieval contracts and SQL constraints | `search_models.py`, `search_filters.py`, `product_search.py` |
| Admission and telemetry | `operations.py`, `operations_config.py`, `metrics.py` |

Keep route handlers thin. Place persistence, inference, validation, and domain
logic in their respective modules. Use explicit imports and type annotations at
service boundaries. Split modules by responsibility rather than arbitrary line
counts. `app.commerce`, established `app.main` exports, and historical prompt imports
from `app.commerce_models` retain compatibility. Internal code should import from
the owning module. A compatibility export or operational CLI is not dead code
merely because runtime request handlers do not call it.

Ruff is not a whole-program reachability proof. Before removing a symbol, search
runtime code, tests, scripts, docs, and CI, and check whether it is a public
compatibility export or discovered through framework registration. Remove only
verified unused definitions; do not prune installed providers or data generations
as part of source cleanup.

## Compatibility

Preserve response schemas, ownership checks, idempotency, atomic turn commits,
and mandatory retrieval constraints during refactors. Prompt wording and literal
user evidence are behavior, not formatting. Change them deliberately and test them.

Embedding input compatibility uses an explicit version and identifier in
`embedding_text.py`. Update both when input semantics change and rebuild affected
indexes. Formatting alone must not invalidate an existing search generation.
Include new ranking/filter modules in the search-cache code signature.

Keep exactly four maintained Markdown files: README.md and docs/architecture.md,
docs/development.md, docs/operations.md. Update these instead of adding reports,
plans or handoff documents. Store machine-readable captures under ignored data/.
Document reusable commands and contracts, and remove stale narrative snapshots.

## Conversation evaluation

Use `scripts/evaluate_conversations.py` to capture configured model responses with
isolated conversation storage. Cases live in `evals/`; section-specific instructions
are in [architecture](architecture.md#conversation-contracts).
`scripts/summarize_evaluation.py --capture CAPTURE.json --review REVIEW.json --output SUMMARY.json`
validates explicit review labels against captured product IDs. Prepare labels for
the actual capture; historical labels cannot be assumed valid after a rebuild.
Report execution, constraint compliance, and reviewed relevance separately, and
identify AI-only reviews as such. Captures and summaries remain under ignored `data/`.

## Catalog import and enrichment

```dotenv
CATALOG_DATABASE_URL=postgresql:///conversational_commerce
CATALOG_ETL_OUTPUT_DIR=../conversational_commerce_etl/data/processed
```

Store database credentials only in the ignored `.env`. CLI diagnostics never
print the connection string or the raw PostgreSQL exception. Relative ETL paths
are resolved against the backend project root, independent of shell directory.

```bash
.venv/bin/python scripts/init_catalog.py --create-database
.venv/bin/python scripts/import_catalog.py
.venv/bin/python scripts/verify_catalog.py
```

Initialization creates the named database only when missing, then applies SQL
migrations. It refuses the PostgreSQL administrative/template databases. It
does not alter roles, server configuration, other databases, Redis, models, or
SQLite. Database creation needs CREATEDB permission; migrations need schema and
table creation privileges in the chosen database. `CATALOG_ADMIN_DATABASE_URL`
may select a separate administrative connection for database creation.

Migration files in `migrations/catalog/` are ordered and checksummed. Never edit
an applied migration; add a new migration. Applied-file checksum mismatches fail.
The migration ledger and import writer lock are scoped to the catalog database.

Run enrichment after the original ETL subset is complete. It keeps the same product
IDs, order, prices, broad families and selected observations. It adds source-backed
attributes to a separate immutable snapshot; it does not expand the selected sample.
The original normalized exports and raw archives are untouched.

The current local demo uses `data/duckdb_demo` as its retained-ID source and
`data/enriched_duckdb` as its output. `CATALOG_ENRICHMENT_OUTPUT_DIR` can override
the default enrichment destination.

```sh
.venv/bin/python scripts/enrich_catalog.py
.venv/bin/python scripts/import_catalog.py --etl-output-dir data/enriched_duckdb --stage
# Use the import_id printed by the preceding command:
.venv/bin/python scripts/build_search_index.py --catalog-import-id IMPORT_ID
.venv/bin/python scripts/verify_catalog.py --etl-output-dir data/enriched_duckdb
.venv/bin/python scripts/verify_search_index.py
```

Staging leaves the serving catalog and search generation in place. The index builder
reuses compatible unchanged vectors, verifies the new generation and publishes both
pointers in one transaction. A failed build retains completed batches for resume;
use both `--catalog-import-id IMPORT_ID` and `--resume-build BUILD_ID`. A concurrent
catalog change prevents publication. Existing snapshots remain available for rollback.

Set `CATALOG_ETL_OUTPUT_DIR=data/enriched_duckdb` for subsequent default import/verification.
`CATALOG_ENRICHMENT_SOURCE_DIR` independently identifies the original ETL output
(default `../conversational_commerce_etl/data/processed`). Source changes require
another enrichment run, staged import and index build.

The pipeline verifies the original subset, compressed archive checksums and row
counts, each selected raw row's SHA-256, and its product ID. It reads only the chosen
observation for that product. The derived run identity includes the source manifest
identity and enrichment code hashes. Its manifest records derivation and checksums;
`enrichment_report.json` records coverage before/after, remaining unknowns and examples.
Publication validates the existing v1 JSON Schema; no upstream schema change is needed.

## Build, refresh and resume embeddings

### Incremental synchronization

```bash
.venv/bin/python scripts/import_catalog.py
.venv/bin/python scripts/build_search_index.py
.venv/bin/python scripts/verify_search_index.py
```

| Change | Embedding work | Index/search behavior |
| --- | --- | --- |
| Identical active build | None | Verify and reuse the serving generation |
| New product | Embed unless an identical compatible input is already available | Add to the next generation |
| Changed selected embedding text | Embed unless the new input already exists in a compatible ready generation | Replace through generation publication |
| Price, provenance, or other metadata only | Reuse the compatible vector | Regenerate the current metadata and keyword row |
| Change only beyond the embedding text budget | Reuse the vector | Regenerate current bounded keyword text |
| Model digest or input contract changes | Do not reuse incompatible vectors | Build a complete compatible generation |
| Product absent from new catalog snapshot | No embedding | Exclude from the next active generation |
| Empty catalog snapshot | No embedding | Publish zero active products |

Reuse requires the exact selected-input SHA-256, model digest, and full input
contract, including dimensions and prompt/text-producer hash. Only retained ready
generations supply cached vectors. An alias for identical model weights may
reuse vectors, but the new build records the configured model name. A B-tree
lookup index supports cache matching. Reuse copies the vector into the new
generation's Parquet batch; it does not depend on the old generation's files or rows.

The keyword title/body and source references come from the **current** catalog
record, never from the cached row. `embedding_reused` records whether a row's
vector was copied. Command results distinguish `embedded_this_run`,
`copied_this_run`, and total `build_reused_vectors`. Counts refer to product rows,
not necessarily distinct input strings.

Incremental refers to model work. Each published generation still stores a
complete snapshot of SQL metadata and Parquet vectors. This uses extra disk while old
generations are retained; it is not in-place mutation of the serving index.
The full source product is preserved even when the embedding/keyword text is
bounded. See [search contracts](architecture.md#search-and-reranking) for limits.

### Re-embedding existing products

```bash
# Replace these example IDs with IDs from the active catalog.
.venv/bin/python scripts/build_search_index.py \
  --reembed-product amazon:YOURASIN01 \
  --reembed-product amazon:YOURASIN02

.venv/bin/python scripts/build_search_index.py --reembed-all
```

Selective refresh forces inference for the requested IDs; other rows can reuse
compatible vectors. Missing IDs fail before creating the refresh. Full refresh
forces inference for every current product, even if the selected text is
unchanged. Both use a distinct generation ID and leave the previous serving
generation untouched until verification and publication succeed.

Every forced invocation starts a new refresh. The script prints its build ID
before processing. Resume that same refresh after an interruption:

```bash
.venv/bin/python scripts/build_search_index.py --resume-build BUILD_ID
```

Committed batches are retained; only missing rows are processed. Resume requires
the same active catalog, model identity, and input contract. It cannot be combined
with force flags. Normal synchronization preserves an already active compatible
refreshed generation instead of reverting to an older default build. Explicitly
resuming a ready generation verifies it and activates it when still compatible.

## Local frontend

Configure the sibling frontend once from the backend checkout:

```bash
.venv/bin/python scripts/setup_local_frontend.py
```

This creates server-only bindings with a dedicated non-admin identity in ignored
credential files and preserves complete existing bindings. Keep PostgreSQL,
Docker/Qdrant, and the configured model services running. Use `127.0.0.1`
consistently because `localhost` has separate browser cookies.

Use two terminals, stopping an older instance yourself before restarting:

```bash
cd /Users/vjaadhi2799/Downloads/conversational_commerce
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
cd /Users/vjaadhi2799/Downloads/querixco
npm run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173/beauty`. Try the shampoo starter, refine its budget,
then ask for a shirt. Open the clothing handoff, send the prefilled request, and
return to beauty: the shampoo preferences should still be there. Check a mixed
request (`shampoo under $15 and shirt for men under $40`), comparison, more
options, refresh, and two tabs with different sections. If a constraint yields
no matching listing, the response should say so without broadening it.

While both servers are running, check the browser-facing gateway and shopping flows:

```bash
.venv/bin/python scripts/check_frontend_live.py
```

The check uses real models and temporary test conversations; it removes those test
conversations afterward. Live products retain historical prices and unknown stock
or variant facts. Optional sample mode is separate and visibly labelled.

## Evaluation boundaries

Use `scripts/check_commerce.py`, `scripts/check_shopping_journeys.py`,
`scripts/check_frontend_live.py` and `scripts/evaluate_conversations.py` only when
live inference is intended: they can use provider quota. Unit tests use mocks.
Use each script's `--help` for supported options; keep captures under ignored data/.
Report execution, constraint compliance and human-reviewed relevance separately.
A small successful sample is not a production latency or answer-quality guarantee.

# Development standards

## Checks

Install `requirements-dev.txt`, then run `make check`. Run `make format` before
committing. Ruff configuration is versioned in `pyproject.toml`; editor defaults
are in `.editorconfig`. Formatting uses four spaces and a 100-character target.
Long SQL, regular expressions, and prompt strings retain their exact contents.

CI checks formatting, imports, undefined names, unused imports/locals/loop bindings,
and redundant lint suppressions, plus tests on Python
3.11 and 3.14. PostgreSQL/pgvector, Qdrant, and Redis run as isolated test services.
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

Keep Markdown factual, concise, and task-focused. Document commands, contracts,
limitations, and operational decisions; avoid duplicated explanations.

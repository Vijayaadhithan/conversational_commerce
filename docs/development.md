# Development standards

## Checks

Install `requirements-dev.txt`, then run `make check`. Run `make format` before
committing. Ruff configuration is versioned in `pyproject.toml`; editor defaults
are in `.editorconfig`. Formatting uses four spaces and a 100-character target.
Long SQL, regular expressions, and prompt strings retain their exact contents.

CI checks formatting, imports, undefined names, unused code, and tests on Python
3.11 and 3.14. PostgreSQL/pgvector and Redis run as isolated test services.
Hosted inference tests use mock transports and require no provider credentials.

For local integration tests, set `CATALOG_TEST_ADMIN_URL` and
`CATALOG_TEST_REDIS_URL`. PostgreSQL tests create and drop a randomly named scratch
database; Redis tests use unique key prefixes. Without these settings, integration
tests are skipped. Use test services, never production credentials.

## Code organization

| Responsibility | Modules |
| --- | --- |
| Application composition and lifecycle | `main.py`, `config.py` |
| HTTP contracts and access checks | `api_models.py`, `api_access.py`, `routes_*.py` |
| Conversation persistence | `conversation_store.py`, `api_security.py` |
| Model calls and turn coordination | `chat_service.py`, `conversation_service.py` |
| Shopping state transitions | `commerce_flow.py`, `commerce_single_turn.py`, `commerce_task_*.py` |
| Preference validation and retrieval intent | `commerce_preferences.py`, `commerce_intent.py`, `commerce_requests.py` |
| Retrieval contracts and SQL constraints | `search_models.py`, `search_filters.py`, `product_search.py` |
| Admission and telemetry | `operations.py`, `operations_config.py`, `metrics.py` |

Keep route handlers thin. Place persistence, inference, validation, and domain
logic in their respective modules. Use explicit imports and type annotations at
service boundaries. Split modules by responsibility rather than arbitrary line
counts. `app.commerce` and existing `app.main` exports retain import compatibility.

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

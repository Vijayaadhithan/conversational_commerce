# Conversational Commerce Backend

For Google AI Studio, GroqCloud, OpenRouter, and Voyage API options, see
[model provider configuration](docs/model_providers.md) and `.env.production.example`.


A local FastAPI backend for persistent conversations with Ollama, a local
1.54B-parameter reranker, and a versioned PostgreSQL fashion/beauty catalog.

- Chat model: `ministral-3:8b` through Ollama.
- Conversation memory: SQLite transcripts, recent turns, and rolling summaries.
- Commerce mode: structured intent extraction, persistent shopping tasks and preferences,
  catalog recommendations with listing-based explanations, and follow-up comparisons/refinements.
- Reranker: [Mixedbread mxbai-rerank-large-v2](https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2),
  running through PyTorch on Apple MPS or CPU.
- Catalog inputs: All Beauty, Amazon Fashion, and Beauty & Personal Care.
- [Listing enrichment and concern explanations](docs/catalog_enrichment.md): versioned
  metadata additions, individual concern evidence, and explicit unknown variant availability.
- [Scaling, evaluation and performance](docs/scaling_and_performance.md): disk-backed
  50,000-product sampling/enrichment, conversation evaluation, index benchmarks,
  bounded Redis caching and optional model warm-up.
- [Local Querix frontend and verified shopping journeys](reports/frontend_live_2026-10-23.md):
  connect the sibling frontend, browse the active catalog, and run the local demo.

`POST /products/search` retrieves products using keyword/vector search and the
local reranker, with structured mandatory filters. Conversations created with
`mode: "commerce"` invoke that pipeline after extracting and validating intent.
`POST /rerank` scores documents supplied by the caller.

## Setup

Use Python 3.12 or later. Apple Silicon is recommended for local GPU reranking.

```bash
git clone https://github.com/Vijayaadhithan/conversational_commerce.git
cd conversational_commerce
bash scripts/setup.sh
```

The setup command creates or reuses `.venv`, installs backend dependencies,
creates a private `.env` from `.env.example` if missing, and checks imports.
It preserves an existing `.env`. Edit that file for local settings; it is ignored
by Git, while `.env.example` contains only shareable defaults. No hosted API key
is required.

Optional setup components:

```bash
# Backend + PostgreSQL/pgvector/Redis + reranker libraries + testing libraries
bash scripts/setup.sh --with-services --with-reranker --dev

# Preview the same setup without installing, writing files, or starting services
bash scripts/setup.sh --with-services --with-reranker --dev --dry-run
```

| Option | Effect |
| --- | --- |
| `--with-services` | Install/reuse PostgreSQL, pgvector, and Redis; start unavailable services and check local ports 5432/6379 |
| `--with-reranker` | Install local reranker Python libraries |
| `--dev` | Install test dependencies |
| `--dry-run` | Print planned changes only |
| `--help` | Show setup options |

Core setup supports macOS/Linux. Automatic system installation uses Homebrew on
macOS or APT on Debian/Ubuntu; Homebrew must already be installed. When Python
3.12+ is missing, setup tries the platform package manager. Older Linux releases
may need Python installed separately. Use `PYTHON_BIN=/path/to/python3.12` to
select a Python executable. An unusable existing `.venv` produces an error rather
than being overwritten.

Service setup reuses the selected PostgreSQL installation and builds pinned
pgvector v0.8.7 against its `pg_config`. This supports existing PostgreSQL 16
installations as well as newer versions without requiring a major-version
upgrade. Git and compiler/development tools are needed for that build; Linux
packages are installed when missing. On macOS, install Xcode Command Line Tools
if requested. System installation may invoke `sudo`; run the script as your
normal user. See the [upstream installation notes](https://github.com/pgvector/pgvector#installation-notes---linux-and-mac).

If multiple PostgreSQL installations are found, choose one explicitly:

```bash
PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config \
  bash scripts/setup.sh --with-services
```

Existing ready services are reused. Custom PostgreSQL distributions need to be
started using their own service manager. Setup checks default local ports;
nondefault ports require manual configuration. It installs pgvector extension
files, but creating a commerce database and enabling `vector` inside that
database remain separate steps.

The installer leaves **Ollama, model weights, and catalog downloads separate**.
The model commands below are optional manual setup and are never invoked by
`setup.sh`.

Install and start [Ollama](https://ollama.com/), then download the chat model:

```bash
ollama pull ministral-3:8b
```

If the Ollama desktop app is not running, start its server in another terminal:

```bash
ollama serve
```

## Run the backend

First provision a user token and set `COMMERCE_API_TOKEN_FILE` in `.env`; see
[API operations](docs/operations.md) for the commands, legacy conversation
ownership, retry keys, limits, monitoring and real load testing. Authentication
and request keys are enabled by default. The terminal client handles both.

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use one Uvicorn worker. Interactive API docs: http://127.0.0.1:8000/docs.
Public liveness: http://127.0.0.1:8000/livez. Health/readiness require a bearer token.

In another terminal, start a continuous conversation:

```bash
.venv/bin/python scripts/chat.py
# Or start a product recommendation conversation:
.venv/bin/python scripts/chat.py --commerce
```

The client prints a conversation ID. `/history` shows the transcript, `/new`
starts another conversation, and `/exit` quits. Resume a saved chat with:

```bash
.venv/bin/python scripts/chat.py --session YOUR_CONVERSATION_ID
```

Commerce mode also supports `/preferences`. It remembers requirements across
turns and restarts, asks for missing product/recipient details, and supports
“under $20 instead,” “more options,” “cheaper,” and “compare the first two.”
See [conversational recommendations](docs/conversations.md) for the API, examples,
state rules, and validation commands.

## Install the local 1.54B reranker

The pinned model download is about 3.1 GB. Weights stay under the ignored
`models/` directory; the backend loads them only when reranking is requested.

```bash
.venv/bin/python -m pip install -r requirements-reranker.txt
.venv/bin/python scripts/download_reranker.py
.venv/bin/python scripts/check_reranker.py
```

`RERANK_DEVICE=auto` selects MPS after a real GPU operation succeeds, otherwise
CPU. Apple GPU loading uses float16; CPU loading uses float32 and needs more
memory. Defaults are batch size 2 and 512 tokens per query/document pair. Longer
pairs are truncated. Limit local concurrency while other models are loaded.

```bash
curl -sS http://127.0.0.1:8000/rerank \
  -H 'Content-Type: application/json' \
  -d '{"query":"fragrance free moisturizer for dry skin","documents":["A leather handbag","Fragrance-free moisturizer for dry skin"],"top_k":2}'
```

The response includes the model, selected device, original document indexes,
raw relevance scores, and documents in descending score order. Scores are not
calibrated probabilities of product suitability.

## Product metadata

```bash
.venv/bin/python scripts/download_catalogs.py
```

The script downloads only the three sources in [data_sources.json](data_sources.json),
verifies expected archive sizes and full gzip/JSON integrity, computes SHA-256,
and writes record counts to `data/public_sources/download_report.json`.

| Source | Validated metadata rows |
| --- | ---: |
| All Beauty | 112,590 |
| Amazon Fashion | 826,108 |
| Beauty & Personal Care | 1,028,914 |
| Total before deduplication | 1,967,612 |

The compressed archives total about 975 MB. These are historical product
metadata, including image URLs; product images and review archives are not
downloaded. Normalization and quality sampling are handled by the separate
[ETL project](https://github.com/Vijayaadhithan/conversational_commerce_etl).
Do not treat historical prices or availability as current commerce facts.
Check source terms before redistributing third-party data or weights.

## PostgreSQL catalog

After running the ETL project, import its quality-eligible subset. The default
paths expect the ETL checkout beside this backend. Configure
`CATALOG_DATABASE_URL` and `CATALOG_ETL_OUTPUT_DIR` in `.env`; see
[.env.example](.env.example). Existing conversation storage remains SQLite.

```bash
# Install the catalog driver and schema-validation dependencies
.venv/bin/python -m pip install -r requirements.txt

# If PostgreSQL/pgvector are missing, install matching service dependencies
bash scripts/setup.sh --with-services

# Create the dedicated database if missing, then apply versioned migrations
.venv/bin/python scripts/init_catalog.py --create-database

# Optional: validate the entire subset without connecting to PostgreSQL
.venv/bin/python scripts/import_catalog.py --check-only

# Import, publish, and independently compare all products with their source
.venv/bin/python scripts/import_catalog.py
.venv/bin/python scripts/verify_catalog.py
```

The default database is `conversational_commerce`. Local socket authentication
uses your current PostgreSQL user; use a password-protected connection URL in
the ignored `.env` for another installation. Creating a database requires the
appropriate PostgreSQL privilege. Omit `--create-database` when an administrator
has already created it; an optional `CATALOG_ADMIN_DATABASE_URL` can select the
administrative connection for creation.

Imports validate the pinned JSON Schema, subset checksum/size/count, IDs,
eligibility, positive historical USD prices, and source-observation references.
Complete normalized records and unknown attributes are preserved. Typed columns
are generated from the same JSONB record for category, brand, and price filters.
The current dataset is available through `commerce.active_products`.

Each completed manifest identifies an immutable snapshot. A single transaction
loads and verifies all products before switching the active snapshot. Failed
imports preserve the previous catalog. Repeating an import verifies and reuses
its records; it does not append duplicates. Older snapshots remain available
for deliberate rollback and are never pruned automatically.

See [docs/catalog.md](docs/catalog.md) for the schema, sample queries, validation
boundaries, and test commands. Build product embeddings as described below.

## Product embeddings and search

The existing local `embeddinggemma:latest` model produces 768-dimensional
embeddings. This model must already be installed; setup and embedding scripts do
not download weights. Adjust `EMBEDDING_MODEL`, `EMBEDDING_BATCH_SIZE`, and
`EMBEDDING_TIMEOUT_SECONDS` in `.env` if needed. The text contract currently uses
EmbeddingGemma retrieval prompts and 768 dimensions; another model may require
a deliberate prompt/schema change, not just a tag replacement.

```bash
.venv/bin/python scripts/init_catalog.py
.venv/bin/python scripts/build_search_index.py
.venv/bin/python scripts/verify_search_index.py

.venv/bin/python scripts/search_products.py 'moisturizer for dry skin' \
  --filters '{"domain":"beauty","family":"skin_care","max_price":"30.00"}'
```

Normal embedding builds commit completed batches and resume on rerun. Across
catalog snapshots, compatible vectors are reused by selected-text hash; only
new/changed embedding inputs need inference. Price-only changes reuse vectors
and refresh catalog/keyword metadata. Removed products are absent from the next
active generation, including support for an empty catalog.

A complete, verified build is published atomically. Model digest, catalog snapshot, and input
contract prevent mixed generations. At this 20,000-product scale, semantic search
uses exact cosine distance; PostgreSQL full-text search provides keyword matches,
and reciprocal rank fusion combines the two candidate rankings.

The existing local reranker scores the best 20 eligible RRF candidates before
selecting the requested results. Reranking is enabled by default and uses the
same lazy model instance as `/rerank`. Install the optional reranker libraries
and local weights using the earlier setup instructions if missing. To explicitly
use the RRF baseline without that dependency:

```bash
.venv/bin/python scripts/search_products.py 'running shoes' \
  --filters '{"domain":"fashion","family":"footwear","max_price":"75.00"}' --no-rerank
```

HTTP accepts `"rerank":false` for the same comparison. A requested rerank that
fails returns 503 instead of silently falling back. `PRODUCT_RERANK_CANDIDATES`
sets the server-side pool size (20-50). See [docs/product_reranking.md](docs/product_reranking.md).

```bash
# Force selected products, or force an entirely new embedding generation
.venv/bin/python scripts/build_search_index.py --reembed-product amazon:YOURASIN01
.venv/bin/python scripts/build_search_index.py --reembed-all

# A failed forced refresh prints its build ID; resume that exact generation
.venv/bin/python scripts/build_search_index.py --resume-build BUILD_ID

# Preview retention; active generation is always protected
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 1
# Add --apply to execute; one inactive ready generation is retained for rollback
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 1 --apply

# Rebuild the database indexes without re-embedding; reclaim deleted space for reuse
.venv/bin/python scripts/manage_search_index.py reindex
.venv/bin/python scripts/manage_search_index.py vacuum
```

Replace placeholder product/build IDs with real IDs. Repeating a force flag
starts another generation; use `--resume-build` to resume one. Explicit build
deletion and retention details are in [docs/embedding_lifecycle.md](docs/embedding_lifecycle.md).

With the backend running:

```bash
curl -sS http://127.0.0.1:8000/products/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"moisturizer for dry skin","filters":{"domain":"beauty","family":"skin_care","max_price":"30.00"},"limit":5}'
```

Mandatory requirements belong in `filters`: relevance text alone does not parse
budgets, exclusions, or suitability requirements. Supported filters include
domain, family, brand, historical USD price bounds, explicit attributes, and
excluded product IDs. Attribute text matching is exact and case-insensitive;
`fragrance_free` requires a boolean. Unknown values never satisfy a required
attribute. Empty matches stay empty; constraints are not relaxed.

Results include RRF/reranker ranking signals, historical price, explicit attribute claims,
listing excerpts, and observation provenance. Similarity and excerpts do not
establish medical suitability or live stock. Conversation-level preference
handling remains a subsequent integration step. See
[docs/search.md](docs/search.md) for build behavior and limitations.

## API

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Check Ollama connectivity and model installation |
| POST | `/conversations` | Create a chat with `{}` or `{"title":"..."}` |
| GET | `/conversations?limit=50&offset=0` | List saved chats |
| GET | `/conversations/{id}` | Read chat details and memory summary |
| POST | `/conversations/{id}/messages` | Send `{"message":"..."}` and receive an answer |
| GET | `/conversations/{id}/messages?after=0&limit=100` | Read messages; paginate with `next_after` |
| DELETE | `/conversations/{id}` | Delete a chat and its messages |
| POST | `/rerank` | Rank up to 50 supplied documents |
| POST | `/products/search` | Search the active catalog using relevance text and mandatory structured filters |

Messages accept up to 4,000 UTF-8 bytes. Responses are returned after generation,
without token streaming. If `done_reason` is `length`, the output limit was
reached; increase `OLLAMA_MAX_TOKENS` or ask the model to continue.

Full conversation transcripts persist across restarts. Older turns are
summarized to keep model input bounded; summaries are lossy. Failed inference
does not save a partial turn or partial memory update.

This backend binds to localhost and uses per-user bearer authentication with
conversation ownership, durable request keys and bounded model admission. Model weights,
catalogs, conversations, `.env`, runtime caches, and local audit notes are
excluded from Git. See [docs/architecture.md](docs/architecture.md) for storage
choices and current implementation boundaries.

## Validation

Automated tests use simulated inference and do not download models:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

With Ollama and the backend running, check actual chat recall over nine turns:

```bash
.venv/bin/python scripts/smoke_test.py
```

That smoke test deletes its temporary conversation. The separate reranker check
runs local scoring and saves its observed device/results under `data/`.

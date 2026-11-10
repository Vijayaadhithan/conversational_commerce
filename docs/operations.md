# Deployment and operations

## Docker deployment

Target: one Linux server with 2 vCPUs, 8 GiB RAM and 100 GB storage. The configured
demo uses NVIDIA generation, Voyage reranking with a local CPU fallback, and local
EmbeddingGemma. The API runs one worker with a 1 GiB memory limit, one admitted
model workload, queue size four and a 210-second graceful shutdown period.
PostgreSQL, Qdrant, Ollama and Caddy run in Docker; Redis is optional and disabled
in this demo. The frontend is a separate Cloudflare Worker.

| Location | Contents |
| --- | --- |
| `/opt/commerce/conversational_commerce` | Backend source, private `.env.server`, runtime data |
| `/opt/commerce/conversational_commerce_etl` | ETL source and retained data; no automatic ETL job |
| `/opt/commerce/state` | Persistent PostgreSQL, Qdrant, Ollama and Caddy state |
| `/opt/commerce/secrets` | Private database secret |
| `/opt/commerce/backups` | Recovery bundles and deployment rollback files |

The API/tools run as UID 10001 with a read-only root filesystem, writable data,
read-only model files, dropped capabilities and bounded logs. Model weights,
credentials and catalog data are not baked into the image. Dependency image digests
are selected from server-local `deploy/images.env`. Host networking is a single-host
tradeoff: dependencies and API bind to loopback; Caddy owns public ports 80/443.

```sh
cd /opt/commerce/conversational_commerce
sh scripts/server_compose.sh ps
sh scripts/server_compose.sh --profile tools run --rm tools
sh scripts/server_compose.sh --profile app build api
sh scripts/server_compose.sh --profile app up -d --no-deps --timeout 210 api
sh scripts/server_compose.sh --profile public up -d proxy
curl --fail https://commerce-api.querix.co/livez
```

The tools preflight checks configuration, not paid quota or full runtime behavior.
Preserve the serving image and private configuration before updates. Configuration
changes require container recreation, not just restart. For provider-only rollback,
restore the previous environment and image, then recreate API; do not restore the
conversation database and discard newly saved messages. Do not run `down -v` or
remove state directories during an ordinary deployment.

### Keep local embeddings warm

The demo's Ollama embedding requests use `keep_alive: "10m"`. After ten minutes
without embedding traffic, the next search must reload EmbeddingGemma. On November
10, 2026, a direct server measurement took 24.38 seconds cold (24.33 seconds loading)
and 0.039 seconds warm. These are embedding timings, not full query timings.

For this Linux deployment, the checked-in systemd timer sends one small synthetic
embedding request every five minutes, with its first activation 90 seconds after
boot. It uses loopback Ollama, requires only Python's standard library, and writes
timings to the system journal. It does not call hosted providers or save conversation
turns. Ollama must already have `embeddinggemma:latest` installed and be running on
`127.0.0.1:11434`. The model occupies about 693 MB while loaded on the measured server.

Install or update the host job from the repository root:

```sh
sudo install -d -m 0755 /usr/local/libexec
sudo install -m 0644 scripts/warm_embedding.py /usr/local/libexec/commerce-warm-embedding.py
sudo install -m 0644 deploy/systemd/commerce-embedding-warmup.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/commerce-embedding-warmup.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/commerce-embedding-warmup.service /etc/systemd/system/commerce-embedding-warmup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now commerce-embedding-warmup.timer
sudo systemctl start commerce-embedding-warmup.service
systemctl list-timers commerce-embedding-warmup.timer --no-pager
sudo journalctl -u commerce-embedding-warmup.service -n 20 --no-pager
```

The service has a 75-second HTTP timeout and a 90-second execution limit. A failed
run is logged; the timer tries again on its next five-minute interval. This prevents
routine idle unloads while Ollama is healthy; it does not eliminate the first load
after a restart or delays from NVIDIA generation, reranking, or other search work.
To stop scheduled warm-ups, run
`sudo systemctl disable --now commerce-embedding-warmup.timer`.

## HTTPS and frontend

`commerce-api.querix.co` points to the commerce server using a DNS-only A record.
Caddy's configuration is `deploy/Caddyfile`; it redirects HTTP to HTTPS, renews
certificates and proxies SSE without buffering. Preserve its certificate state.
The separate Personal RAG/analytics origin `api.querix.co` must not be repointed.
`/ops/*`, `/docs`, `/redoc` and `/openapi.json` are blocked at the public edge.
Authenticated operations remain available through loopback or SSH.

The Querix Worker needs encrypted runtime secrets `COMMERCE_API_URL`,
`COMMERCE_API_TOKEN` (dedicated non-admin identity), and `COMMERCE_SESSION_SECRET`.
Provider keys stay on the backend. Local ignored `.dev.vars` is not production
configuration. Keep existing unrelated Worker bindings when deploying with Wrangler.
The hosted frontend does not depend on a local SSH tunnel.

For local frontend access to the remote API, run `sh scripts/connect_demo_server.sh`
and keep it open. In the sibling querixco directory run
`npm run dev -- --host 127.0.0.1 --port 5173`. Stop any local API already occupying
port 8000 before opening the tunnel. Frontend `/api/commerce/status` checks bindings,
not upstream health; diagnose failures with authenticated backend readiness.

## Model providers

Choose chat, embeddings, and reranking independently. The checked-in `.env.example` defaults to Ollama and the local reranker;
existing private environments can select hosted providers. `.env.production.example`
is a hosted template. No provider SDK or local model runtime is needed when all
three components use hosted APIs; `requirements.txt` is sufficient.

| Component | Selector | Model setting | Credential |
| --- | --- | --- | --- |
| Local chat | `LLM_PROVIDER=ollama` | `OLLAMA_MODEL` | None |
| Google AI Studio chat | `LLM_PROVIDER=google` | `LLM_MODEL` | `GOOGLE_API_KEY` |
| NVIDIA chat | `LLM_PROVIDER=nvidia` | `LLM_MODEL` | `NVIDIA_API_KEY` |
| GroqCloud chat | `LLM_PROVIDER=groq` | `LLM_MODEL` | `GROQ_API_KEY` |
| OpenRouter chat | `LLM_PROVIDER=openrouter` | `LLM_MODEL` (vendor/model ID) | `OPENROUTER_API_KEY` |
| Local embeddings | `EMBEDDING_PROVIDER=ollama` | `EMBEDDING_MODEL` | None |
| Google AI Studio embeddings | `EMBEDDING_PROVIDER=google` | `GOOGLE_EMBEDDING_MODEL=gemini-embedding-001` | `GOOGLE_API_KEY` |
| Local reranking | `RERANK_PROVIDER=local` | `RERANK_MODEL_PATH` | None |
| Voyage reranking | `RERANK_PROVIDER=voyage` | `VOYAGE_RERANK_MODEL=rerank-2.5` | `VOYAGE_API_KEY` |

For example, set the following in the deployment environment or ignored `.env`:

```dotenv
LLM_PROVIDER=google
# Set an actual chat model ID enabled in your Google AI Studio account.
LLM_MODEL=
LLM_TIMEOUT_SECONDS=120
LLM_CONTEXT_SIZE=16384
LLM_MAX_TOKENS=2048
EMBEDDING_PROVIDER=google
GOOGLE_EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_REVISION=1
RERANK_PROVIDER=voyage
VOYAGE_RERANK_MODEL=rerank-2.5
RERANK_TIMEOUT_SECONDS=60
GOOGLE_API_KEY_FILE=/run/secrets/google_api_key
VOYAGE_API_KEY_FILE=/run/secrets/voyage_api_key
API_AUTH_REQUIRED=1
API_REQUIRE_IDEMPOTENCY_KEY=1
```

An empty hosted `LLM_MODEL` deliberately fails startup until you choose a model.
To switch chat to NVIDIA, GroqCloud or OpenRouter, change `LLM_PROVIDER` and `LLM_MODEL`,
and provide that provider's credential. Google embeddings and Voyage reranking
continue using their own credentials. Select a chat model supporting system
messages, temperature, and JSON object output. Provider-specific reasoning token
budgets may require a larger `LLM_MAX_TOKENS`; truncated output is rejected.

`LLM_CONTEXT_SIZE` controls the app's conservative prompt budget; set it within
the selected model's capacity. In Ollama mode it also configures `num_ctx`.
Blank `LLM_*` limits inherit their corresponding existing `OLLAMA_*` values.
Restart the API after configuration changes. There is no automatic generation-provider fallback; Voyage can use the CPU
reranker fallback described below. Invalid shopping intent gets at most one repair call; transport,
authentication and rate-limit errors do not trigger that repair. Optional commerce
summaries add one bounded call through the same configured provider. Ollama uses
NDJSON streaming; hosted adapters consume compatible SSE. Provider failure falls
back to the deterministic product reply without switching providers.

### Embedding migration

The catalog uses 768-dimensional vectors. Google embeddings use native
`RETRIEVAL_DOCUMENT` and `RETRIEVAL_QUERY` task types with normalization. This
adapter currently supports `gemini-embedding-001`; other embedding models need
an explicit input-contract implementation. Ollama's existing query prefix and
local weight-digest verification remain unchanged.

After configuring Google and its key, build a new generation before serving
queries with the changed configuration:

```sh
.venv/bin/python scripts/build_search_index.py
```

The builder selects the configured provider, retains completed batches for
resume, and publishes the completed generation atomically. Switching provider
requires new embeddings for the entire catalog and incurs provider usage.
Schedule the build/cutover in a maintenance window or use a separate deployment
and catalog database: old workers cannot serve a newly published incompatible
embedding generation. Rollback must restore a compatible configuration and
search generation together. Never reuse local vectors with Google query vectors.

Hosted embedding identity hashes provider, model, dimensions, request contract,
and `EMBEDDING_REVISION`. It is **not a remote weight checksum**. If the provider
changes weights behind an unchanged model ID, increment `EMBEDDING_REVISION`
and rebuild; the application cannot independently detect that change. Search
cache identity also includes the selected reranker provider and model.

### Credentials and serving security

For each provider, set either `*_API_KEY` or `*_API_KEY_FILE`, never both. Secret
files may be mounted by your deployment secret manager. Missing selected-provider
credentials fail configuration early. Unselected providers need no credentials.
`.env` is ignored and has owner-only permissions; tracked templates contain no
secrets. Actual keys must stay out of version control, command arguments, and
shared terminal output.

Hosted requests use fixed HTTPS endpoints with certificate verification,
connection/read timeouts, redirects disabled, and environment proxy inheritance
disabled. Keys travel in headers, never query strings. Upstream error bodies,
request headers, and prompts are not included in API error responses. OpenRouter
requests deny provider data collection, require parameter support, and disable
provider fallbacks. These routing flags are not a guarantee of zero data retention;
review account settings and provider policies for your deployment.

Existing bearer authentication, ownership isolation, durable idempotency,
request limits, model admission limits, and intent/evidence validation still
apply. Keep authentication and idempotency enabled in production. Expose the
application behind HTTPS, provision client tokens with `scripts/manage_api_users.py`,
and run one worker per conversation SQLite database; see [access control](#access-control).
Provider API keys are separate from tokens used by clients of this application.

Queries/catalog excerpts go to the selected embedding/reranking provider;
conversation context goes to the selected LLM provider. This is an explicit
change in data destination when hosted mode is enabled.

### Readiness and validation

`/health` checks the selected chat provider's model listing. `/ready` checks
local models or Google embedding metadata, the active search generation, and
local reranker files/runtime or Voyage configuration. `/ops/status` labels the
Voyage check `configuration_only` with `inference_verified=false`: no free Voyage
model/credential probe is assumed. Listing/configuration probes do **not** verify
inference quota, model compatibility, or paid account access.

`SEARCH_WARMUP=1` performs real embedding and reranking calls at startup and can
incur charges. Verify the selected chat model with an authenticated conversation
before production traffic. `scripts/check_reranker.py` also uses the selected
reranker and performs real inference. Tests in `tests/test_providers.py` use mock
HTTP transports, so they do not consume quota or prove live account access.

Protocols follow the official [Google chat compatibility API](https://ai.google.dev/gemini-api/docs/openai),
[Google embeddings API](https://ai.google.dev/api/embeddings),
[Groq chat API](https://console.groq.com/docs/api-reference),
[OpenRouter API](https://openrouter.ai/docs/api-reference/overview),
[OpenRouter routing controls](https://openrouter.ai/docs/guides/routing/provider-selection),
and [Voyage reranking API](https://docs.voyageai.com/reference/reranker-api).

### NVIDIA and CPU outage fallback

NVIDIA uses the fixed `https://integrate.api.nvidia.com/v1/` endpoint.
The demo selects `nvidia/nemotron-3-ultra-550b-a55b` with thinking disabled
for bounded structured plans and responses. JSON validation, usage caps,
request deadlines and sanitized errors apply to this provider too.
No automatic billed generation retries or cross-provider generation fallback are added.

For Voyage outages, install `requirements-fallback.lock` (constrained by
`requirements.lock`), run `python scripts/download_fallback_reranker.py`,
and set `RERANK_FALLBACK_ENABLED=1`. The small pinned
`cross-encoder/ms-marco-MiniLM-L6-v2` ONNX model runs offline on one CPU thread,
in batches of two. The existing Mixedbread local model remains available through
`RERANK_PROVIDER=local`; it is not loaded by the outage fallback.

API transport errors, failed status responses, malformed rankings and an open
Voyage circuit use local ranking. Application spending caps and exhausted turn
deadlines still stop the request. Successful later Voyage calls resume hosted
ranking. Provider output identifies the actual model used; authenticated dependency
health includes fallback load state, successful fallback count and last backend.
Only document text is truncated at 512 tokens; oversized queries fail explicitly.
This smaller English reranker can produce different rankings from Voyage.

Run `python scripts/check_fallback_reranker.py` to verify real CPU inference through
a simulated hosted outage without sending paid requests. Docker mounts `./models`
read-only and includes the CPU runtime, retaining the API's 1 GiB limit.

## Access control

```sh
.venv/bin/python scripts/manage_api_users.py issue --name local-user --token-file data/credentials/local-user.token
.venv/bin/python scripts/manage_api_users.py issue --name local-admin --role admin --token-file data/credentials/local-admin.token
```

Set `COMMERCE_API_TOKEN_FILE=data/credentials/local-user.token` in `.env` for the
terminal client. Files are created with mode 0600 and are never overwritten;
tokens are not printed. The database stores SHA-256 digests of randomly generated
256-bit tokens. Provision each person with a separate identity/token through the
local administrator CLI. Additional tokens for the same name share ownership
and rate limits. There is no public signup endpoint. Existing tokens remain valid
until revoked unless they have an expiry. New CLI
tokens expire after seven days by default (`--expires-days`). To rotate, issue a
replacement into a new private file, switch clients, then revoke the old token ID.
`tokens` lists safe token metadata; `revoke --token-id ID` revokes one token, while
`revoke --name NAME` revokes all tokens for that user. User CLI tokens default to
shopping scope; use `--scopes shopping,rerank` when direct reranking is required.

```sh
.venv/bin/python scripts/manage_api_users.py list
.venv/bin/python scripts/manage_api_users.py revoke --name local-user
```

Send `Authorization: Bearer <token>` to API endpoints. `/livez`, API documentation
and the OpenAPI schema are public; `/health` and `/ready` require authentication.
Use **Authorize** in `/docs`. Administrator tokens can read `/ops/status` and
`/ops/metrics`. Administrators do not bypass conversation ownership. Foreign or
unknown conversation IDs return 404, including preferences, transcripts,
message submission and deletion. Lists contain only the caller's conversations.
Missing, invalid or revoked tokens return 401 (repeated invalid requests are
throttled). The application does not trust user IDs or forwarded-IP headers.

Existing conversations receive a nullable owner in an additive migration. They
are preserved but hidden in authenticated mode until an administrator explicitly
assigns all unowned conversations:

```sh
.venv/bin/python scripts/manage_api_users.py adopt-legacy --name local-user
```

Only run that command when those legacy conversations belong to that user. It
does not reassign conversations that already have an owner. `API_AUTH_REQUIRED=0`
is an explicit local-development escape hatch; it must not be used for a shared
service. Keep localhost binding for local use; a remotely exposed deployment
needs HTTPS termination and protection of the database, token files and host.

### Retry contract

Both `POST /conversations` and `POST /conversations/{id}/messages` require an
`Idempotency-Key`: 8–128 letters, digits, `.`, `_`, `:`, or `-`. A UUID is suitable.
Generate a new key for each logical operation. Reuse the key **and identical
body** after timeouts or ambiguous disconnects. Keys are scoped to identity and
operation/conversation, so different users cannot replay each other's results.

- A completed retry returns the original status/body/message ID with
  `Idempotency-Replayed: true`; it consumes no model budget.
- A concurrent duplicate returns 409 and `Retry-After: 2` while the first works.
- Reusing a key with a different body returns 409 without running the model.
- Pre-commit failures release the claim; a same-key retry may run again.
- Completed responses and the message pair/state commit in **one SQLite
  transaction**. Completed keys survive restarts and have no automatic expiry.
- After a process crash, the sole new worker clears unfinished claims. Inference
  interrupted before commit may run again, but a committed turn is not saved
  twice. This is not an exactly-once guarantee for an external model service.

Different keys with identical text represent separate intentional messages.
Ownership is checked before replay; deleting a conversation makes its message
replays inaccessible. A replay of conversation creation returns the original ID,
unless that conversation has been deleted or the replay payload expired; those
keys return 410. Small tombstones prevent old keys from executing again. Retention
is an explicit maintenance command, not an automatic background purge.

The terminal client reads the token file, retries transient failures with the
same key, and provides `/retry` for its last unresolved message. This pending key
is in client memory; independent integrations should retain keys across their
own restarts. Existing evaluation scripts provision isolated authenticated users
in temporary databases. Their high evaluation rate limits are explicit.

## Rate limits and concurrency

| Setting | Default | Meaning |
| --- | ---: | --- |
| `API_REQUESTS_PER_MINUTE` / `API_REQUEST_BURST` | 120 / 30 | Per-user HTTP token bucket |
| `API_MODEL_REQUESTS_PER_MINUTE` / `API_MODEL_BURST` | 30 / 8 | New message/search/rerank workload bucket |
| `API_MODEL_CONCURRENCY` | 1 | Running workloads; configurable from 1 to 4 |
| `API_MODEL_QUEUE_SIZE` | 8 | Additional waiting workloads |
| `API_MODEL_QUEUE_TIMEOUT_SECONDS` | 120 | Maximum admission wait |

Rates refill continuously; they are not fixed minute windows. 429 means a rate
limit; 503 with `Retry-After` means queue full/expired. Client retries should use
backoff and the same request key. Limits are per process and reset on restart;
local workloads outside this API are not governed by these limits. Bucket memory
is bounded to 4096 identities, so dormant entries may be evicted.

Every model path shares admission, including chat, shopping, embeddings/search
and direct reranking. Separate locks serialize each conversation's mutations.
A cancelled HTTP coroutine retains its admission slot until model/thread work
finishes, allowing a later retry to recover the saved result. Requests are capped
at 256 KiB before JSON parsing, with a 15-second timeout between body chunks.
Keep concurrency 1 on the measured local Mac unless a new benchmark supports a
higher setting. Do not use multiple workers to bypass this budget.

## Monitoring and alerts

`/livez` reports process liveness. `/health` retains the chat-model check.
`/ready` checks SQLite, the active PostgreSQL search generation, installed chat
and embedding models, and reranker file/runtime presence. It returns 503 if a
required dependency (including Qdrant) is unavailable. Redis is optional and fails
open; a Redis
outage raises an alert but does not alone make the API unready. Reranker presence
is not an inference health test; enable `SEARCH_WARMUP=1` to run an actual
embedding/reranking probe before startup completes.

`/ops/status` reports dependency timestamps, active alerts, request-window p95 and
error rate, active/queued work, RSS, CPU time, threads and uptime. Background
probes run every `API_MONITOR_INTERVAL_SECONDS=30`; zero disables the loop for
tests. PostgreSQL connection/query and Ollama probe timeouts are bounded at three
seconds; Redis probes use the cache's short socket timeouts.

`/ops/metrics` exports Prometheus text for HTTP counts/statuses/route templates,
latency histogram, model calls/time/failures, rejected/queued work, idempotency
replays, Redis hits/misses/failures, dependencies and resources. Metrics are local
to this process and reset at restart. Route labels never contain conversation
UUIDs or query text. JSON request logs include a generated `X-Request-ID`, route,
status and elapsed time; they exclude tokens, user messages and raw URLs.

Alert transitions are emitted immediately by the background monitor as JSON
`alert_state_changed` logs and are available in the status/metrics endpoints.
They cover dependency outages, queue saturation, high memory, and a five-minute
request window (bounded to 10,000 samples). Defaults: at least 10 workload samples
and 10% HTTP 5xx, p95 >= 60 seconds, process RSS >= 8192 MiB. Configure the
`API_ALERT_*` settings in `.env.example`. Probes do not dilute workload metrics.

`monitoring/prometheus.yml` and `monitoring/alerts.yml` provide scrape/rule
examples, including API-down detection outside the process. Put the admin token
in Prometheus's bearer token file. Alertmanager/email/Slack delivery is not
configured by the application; this change provides local active alerts and
rules for an external monitoring service. RSS does not include all GPU memory.

## Resource and provider budgets

| Setting | Local default | Meaning |
| --- | ---: | --- |
| `API_BODY_TIMEOUT_SECONDS` | 30 | Total request upload time; the 15-second inter-chunk bound also remains |
| `API_RESPONSE_WRITE_TIMEOUT_SECONDS` | 30 | Maximum wait for each ASGI response write |
| `API_TURN_TIMEOUT_SECONDS` | 180 | Queue + processing deadline for message/search/rerank work |
| `API_HTTP_CONCURRENCY` | 64 | Maximum active HTTP requests, including streams and uploads |
| `API_USER_WORKLOAD_LIMIT` | 4 | Maximum admitted + waiting model workloads per user |
| `API_PREAUTH_REQUESTS_PER_MINUTE` | 600 | Peer bucket before token/database lookup |
| `API_PREAUTH_BURST` | 120 | Peer burst before token/database lookup |
| `API_HEALTH_CACHE_SECONDS` | 10 | Reuse recent dependency probes, including failed health results |

Pre-authentication limits use the ASGI peer address. Arbitrary forwarded headers
are not trusted by this middleware. When a reverse proxy is the observed peer,
this is an aggregate limit: configure proxy-side client limits and explicitly
trusted proxy handling during deployment. Keep application ports private.

SQLite HTTP operations use at most four storage threads. PostgreSQL search
statements have a 10-second statement timeout and 3-second lock timeout. Catalog
summary counts are cached for at most 60 seconds, keyed by database and active
search build, with eight entries maximum. Each lookup still pins the active build.
No retrieval-ranking or eligibility behavior was changed.

Hosted LLM/reranker JSON responses are bounded to 1 MiB before JSON decoding;
summary streams are bounded to 64 KiB before line parsing. Streaming progress
uses a bounded queue, coalesces adjacent text, and drops unnecessary events after
disconnect. If a slow consumer overflows the text budget, the terminal error
instructs same-key recovery; the committed JSON response remains recoverable.

Each hosted provider has a per-process circuit breaker: five failures trigger a
30-second cooldown, then one probe request is admitted. This adds no automatic
paid retries and no provider fallback. Breaker state resets on process restart.
Readiness now requires Qdrant. Provider discovery/configuration probes still do
not prove available quota or successful inference.

The application bounds work after ASGI receives a request. Proxy/Uvicorn
connection limits, header limits, TLS, and firewall rules are still deployment
responsibilities.

### Paid API budgets

`provider_usage` records hosted generation, summary, and reranking calls without
prompts, user messages, credentials, or raw provider responses. Embeddings and
standalone offline scripts are outside this ledger. `/ops/usage` is admin-only.

Reservations are written before dispatch and survive restarts. The default caps
are `API_DAILY_PROVIDER_CALLS=1000` and `API_USER_DAILY_PROVIDER_CALLS=200`, using
UTC calendar days. Replayed completed messages do not reserve another call.
Failed/ambiguous calls still consume call allowance. Unreported token usage stays
unknown; it is not treated as free.

Optional `API_DAILY_ESTIMATED_USD` and `API_USER_DAILY_ESTIMATED_USD` limits default
to zero (disabled). Enabling either requires positive `LLM_CALL_RESERVE_USD` and
`RERANK_CALL_RESERVE_USD` values. Select conservative per-call reserves for the
configured models and maximum inputs. Optional `LLM_INPUT_USD_PER_MILLION` and
`LLM_OUTPUT_USD_PER_MILLION` rates reconcile reported token counts; otherwise the
reservation remains charged. Reranking retains its configured estimate.

These are estimated application budgets, **not guaranteed provider billing caps**.
Incorrect pricing or insufficient reservations can underestimate costs. Actual
usage exceeding a reservation is recorded and blocks later work once the budget
is exhausted. Configure provider-account spending controls separately. Interrupted
requests retain their reservation even if their actual billing is unknown.

## Retention and SQLite recovery

Preview only, without starting the application or modifying its database:

```sh
.venv/bin/python scripts/manage_conversations.py retention \
  --conversation-days 30 --replay-days 7
```

Review the exact conversation IDs, back up, and stop the API before repeating with
`--apply`. Applying retention takes the same exclusive worker lock as the API.
It deletes selected conversations and expires completed replay payloads, while
preserving tombstones, credentials, usage accounting, and catalog data. No
automatic retention schedule or backup deletion is enabled.

```sh
.venv/bin/python scripts/manage_conversations.py backup \
  --output /safe/private/location/conversations.sqlite3
.venv/bin/python scripts/manage_conversations.py \
  --database /safe/private/location/restored.sqlite3 restore \
  --source /safe/private/location/conversations.sqlite3
```

Backup uses SQLite's online backup API with integrity/foreign-key checks. The
output file is private and must not already exist. Restore only accepts a new
database path; it never replaces a serving database or its WAL files. Existing
credentials in a restored database are still sensitive and may require rotation.

## Coordinated catalog recovery bundle

Stop the API, then run:

```sh
.venv/bin/python scripts/backup_demo.py --output /safe/private/location/demo-bundle
.venv/bin/python scripts/backup_demo.py --verify /safe/private/location/demo-bundle
```

This tool takes the worker lock plus existing catalog/build maintenance locks,
backs up SQLite, runs a custom-format `pg_dump`, copies registered Parquet build
directories, and writes a checksum manifest only after completion. It requires a
compatible `pg_dump` (`PG_DUMP_BIN` may select it). Credentials are passed through
libpq environment variables, not process arguments or printed errors. Failed
partial bundles remain for inspection and have no completion manifest.

The bundle intentionally stores the inputs needed to rebuild Qdrant rather than
a Qdrant snapshot. Recovery therefore includes index construction time, but does
not require new embedding calls. Keep compatible application images/source and
secrets separately; the bundle is data recovery, not a complete machine image.

Restore procedure, in an isolated environment first:

1. Verify the bundle, provision a new PostgreSQL database, and restore
   `catalog.dump` with a compatible `pg_restore --exit-on-error --no-owner
   --no-acl`. Use a private libpq service/passfile; never paste credentials into
   command arguments. Do not restore over the serving catalog.
2. Restore SQLite into a new path using the command above. Copy `embeddings/`
   into a dedicated directory and set `SEARCH_ARTIFACT_DIR` to it.
3. Point `CATALOG_DATABASE_URL` at the restored database. Choose a **new**
   `QDRANT_COLLECTION_PREFIX` and compatible Qdrant server. The new prefix avoids
   stale ready collection registrations from the original server.
4. Run `scripts/build_qdrant_index.py`, then `--verify-only`, for the active build;
   repeat with `--build-id` for any rollback build you intend to preserve.
5. Confirm the configured embedding runtime matches the restored vector identity.
   Verify ownership, same-key replay, search readiness and representative shopping
   flows before changing traffic to the recovered deployment.

Copy complete bundles to private encrypted off-host storage. Retain a chosen
backup window (for example seven daily backups) and apply deletion only after
reviewing candidates. Local checksums detect corruption, not malicious replacement
of both files and manifest. Protect access to backups as carefully as the live DB.

SQLite round-trip tests run against temporary data. Full PostgreSQL/Qdrant recovery,
off-host delivery and measured recovery time require a separate service exercise.

## Retention policy

- Preserve every active generation and its input artifacts.
- Preserve one previous verified serving generation, including its catalog, vectors,
  Qdrant collection, and enriched snapshot, through the demo acceptance/rollback window.
- Preserve unfinished builds by default. Inspect ownership and stop writers before
  selecting an abandoned staging directory for deletion.
- Keep newly staged catalog imports for at least seven days unless explicitly selected.
  An import referenced by any search build is never eligible for catalog pruning.
- After each verified publication, review a dry run. There is no background deletion job.
  ETL artifacts need an explicit path allowlist and backend-reference check; age alone
  cannot establish that an output is unused.
- Preserve raw sources, active JSONL and Parquet, credentials, conversation state, and
  required model files. Preserve backend Parquet embedding snapshots together with
  their PostgreSQL build metadata; these are required for model-free Qdrant rebuilds.

## Database maintenance

From the backend repository, preview removal while retaining the active build and
the newest inactive ready build:

```bash
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 1
.venv/bin/python scripts/manage_search_index.py prune-catalog --min-age-days 7
```

Inspect the IDs and preserved reasons before repeating either command with `--apply`.
Run build cleanup first, then preview catalog cleanup again: removing builds can make
additional imports eligible. Catalog pruning also supports repeated `--import-id ID`
for a reviewed explicit selection; IDs override the age grace period, never references.

If selected builds have registered Qdrant collections, applying without
`--remove-qdrant` is refused. Before using that option, stop API readers and wait for
in-flight requests to finish; advisory maintenance locks exclude writers, not readers.
Remote collections are removed before their registry entries. Aliased collections are
refused. A remote failure rolls back SQL changes; retry tolerates an already missing
collection. This is retryable cleanup, not an atomic transaction across both systems.
Unregistered collections require separate inspection and are never inferred/deleted.
After build deletion commits, use `manage_search_index.py prune-artifacts` to preview
unreferenced embedding directories, then repeat with `--apply`. Registered builds,
including incomplete builds, are protected. Use a dedicated `SEARCH_ARTIFACT_DIR`
for each database; backups must include both this directory and PostgreSQL metadata.

```bash
.venv/bin/python scripts/manage_search_index.py vacuum
```

Regular vacuum makes deleted space reusable; it does not generally return all space
to the operating system. Do not schedule `VACUUM FULL` automatically: it requires
exclusive locks and extra temporary disk space. Measure remaining bloat first.

## Release checks and remaining limits

`make check` runs lint, formatting checks and tests. Linux CI tests isolated
PostgreSQL/Qdrant/Redis services. Security CI audits both runtime lockfiles and
scans Git history with redacted Gitleaks output. A reviewed historical Docker-tag
false positive has one exact fingerprint in `.gitleaksignore`; no secret rule or
whole file is disabled. New findings must be reviewed individually.

Keep authentication, idempotency and daily provider call caps enabled. Environment
and secret files must be private (0600), ignored by Git and excluded from Docker
builds. Never print expanded Compose configuration or raw environments in logs.
`requirements.lock` and `requirements-fallback.lock` pin versions, not artifact hashes.
Dependency and Actions updates are manual; Dependabot is not configured.

Pushes to main, PRs and manual dispatch run checks; security also runs weekly.
Each push creates new runs. Concurrency cancels older unfinished runs on the same
branch/PR; completed runs remain in history. Matching pip caches are reused, not
completed job results. No build artifacts are uploaded. Avoid redundant reruns.

Automated off-host backups, external alert delivery, private catalog tenant scope,
multiple API replicas, broad human relevance evaluation and sustained capacity
validation remain separate work. Local backup files alone do not protect against
server loss. `scripts/load_test.py` uses real models and temporary conversations;
run it deliberately, and report failures as well as latency.

## Private tester accounts

Bootstrap once through trusted server access, using a private output file:

```bash
sh scripts/server_compose.sh --profile tools run --rm --no-deps tools \
  python scripts/bootstrap_portal.py --username admin \
  --output data/credentials/portal-admin-initial.json
```

Sign in at `https://querix.co/commerce/login` and replace the temporary password.
Remove the initial credentials file after changing it. The admin panel at
`/commerce/admin` creates users, changes their category grants, disables accounts,
resets passwords, and deletes accounts with optional history deletion. Deletion
retains a disabled identity tombstone for audit/usage accounting and reserves the
username; it never retains a usable password. Existing backups are unaffected.

The frontend Worker service identity must have `portal` scope only. It may call
`/portal/login` but cannot search or access conversations. Browser shopping and
admin calls carry individual portal sessions; operational API tokens stay separate.
Private Worker bindings and model/provider environment settings do not change.
Portal accounts cannot bypass password/session revocation using legacy API tokens.

Sessions expire after eight hours, with up to five concurrent sessions per user.
Password change/reset and account disabling revoke all sessions. Category changes
are checked on every new request, including history and replay, and again before
committing a commerce turn. Previously delivered data cannot be recalled. The UI
refreshes permissions on focus and every 30 seconds. New accounts require a password
change before shopping/admin actions. Password hashes use Argon2id (19 MiB, two
iterations, one lane); two concurrent hashing operations bound CPU/RAM use. Login
is limited globally and to five attempts per username per 15-minute window.

Back up the SQLite database consistently: it now contains account hashes, sessions,
grants and the audit trail as well as conversation state. Never delete it to clear
chat history. During rollback preserve the additive portal tables and keep the
Worker token restricted; rolling back to anonymous browsing requires explicit review.

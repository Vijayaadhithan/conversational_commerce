# API operations

Hosted model credentials, readiness limits, and provider selection are documented
in [model providers](model_providers.md).


The API supports local bearer identities, conversation ownership, durable retry
keys, per-user rates, bounded model admission, metrics and active alerts. Run
**one Uvicorn worker per conversation SQLite database**. An exclusive OS file
lock refuses a second worker. Distributed admission and identity federation are
not implemented. Backups and recovery are outside this change.

## First use and access control

```sh
.venv/bin/python scripts/manage_api_users.py issue --name local-user --token-file data/credentials/local-user.token
.venv/bin/python scripts/manage_api_users.py issue --name local-admin --role admin --token-file data/credentials/local-admin.token
```

Set `COMMERCE_API_TOKEN_FILE=data/credentials/local-user.token` in `.env` for the
terminal client. Files are created with mode 0600 and are never overwritten;
tokens are not printed. The database stores SHA-256 digests of randomly generated
256-bit tokens. Provision each person with a separate identity/token through the
local administrator CLI. Additional tokens for the same name share ownership
and rate limits. There is no public signup endpoint. Tokens remain valid until
revoked. To rotate, revoke existing tokens and issue a replacement into a new
private file, then switch clients. `revoke` revokes **all** tokens belonging to
the named user; individual-token revocation is not exposed by the CLI.

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

## Retry contract

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
including when that conversation has since been deleted. Durable request rows
use disk over time; no automatic retention purge is implemented.

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
required dependency is unavailable. Redis is optional and fails open; a Redis
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

## Repeatable real load tests

```sh
.venv/bin/python scripts/load_test.py --output data/operations/load.json --queue-size 4
# Repeat a larger sample or compare with caching disabled:
.venv/bin/python scripts/load_test.py --output data/operations/load_repeat.json --rounds 3 --queue-size 4
.venv/bin/python scripts/load_test.py --output data/operations/load_no_cache.json --disable-cache --queue-size 4
```

This starts one temporary authenticated API on a loopback ephemeral port, using
real local models and the active catalog. It runs multi-turn shopping sessions
at concurrency 1/2/4, retries and access-isolation checks, and deliberate queue
overload. It measures HTTP latency/status/throughput, samples API and shared
Ollama RSS every 0.5 seconds, and records actual Redis counter deltas in an
isolated cache namespace. Normal rate limits are raised for this test to measure
model queue capacity; deterministic tests verify the separate 429 behavior.
Only its own child API is stopped. Shared Ollama/PostgreSQL/Redis stay running.
The temporary conversation DB is removed; benchmark cache entries expire by TTL.

Cold startup includes embedding/reranker warm-up; the first real planner request
still includes cold chat-model loading. OS/database caches are not flushed.
These short runs measure this machine and workload, not production SLOs. Small
sample p95 values are often the maximum. Include failed requests and overload
counts when interpreting throughput. The JSON, server log and Prometheus snapshot
are under the supplied ignored `data/` output path.

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
  required model files. Current rebuild/verification code still uses PostgreSQL vectors.

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

```bash
.venv/bin/python scripts/manage_search_index.py vacuum
```

Regular vacuum makes deleted space reusable; it does not generally return all space
to the operating system. Do not schedule `VACUUM FULL` automatically: it requires
exclusive locks and extra temporary disk space. Measure remaining bloat first.


Cleanup audit manifests/results remain under `data/operations/cleanup_2026-10-26/`.
Completed one-off cleanup programs and narrative reports have been removed. The reusable
maintenance commands above are the supported entry points; no background deletion job runs.

## Follow-up work carried forward

The previous cleanup report identified business-scope/off-topic enforcement, malformed
planner recovery, evidence-constrained generated summaries, full destination-session
handoff evaluation, dependency locking, and backup/restore exercises. This source cleanup
does not claim those product/production improvements are complete. Storefront filtering
and mechanical test success alone do not establish these guarantees.

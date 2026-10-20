# API operations implementation and measured load test — 2026-10-20

Implemented the five requested areas: rate/admission limits, durable request
deduplication, monitoring/alerts, realistic concurrent testing, and authenticated
conversation ownership. Backups and recovery are excluded as requested.

## Delivered behavior

- **Authentication:** independently provisioned bearer identities, hashed tokens,
  revocation, user/admin roles, owner-only conversation lists, reads, preferences,
  messages and deletion. Administrators cannot read another user's conversations.
  The normal terminal client reads a private token file; Swagger documents bearer
  authorization and request-key headers.
- **Limits:** per-user HTTP and workload token buckets return 429 with retry
  guidance. A shared model semaphore and bounded waiting queue reject excess
  work with 503. Conversation locks prevent overlapping state changes. Cancelled
  requests retain capacity until underlying work finishes. One worker per SQLite
  database is enforced by an OS lock.
- **Deduplication:** creation and message submission require `Idempotency-Key`.
  Completed replies are committed with the turn in the same SQLite transaction,
  survive restart, and replay without generating/saving another turn. Concurrent
  duplicates and changed bodies return 409. Failed transactions roll back the
  message pair, state and response together.
- **Monitoring:** structured request logs and request IDs, authenticated readiness,
  admin metrics/status, bounded request latency/error windows, dependency checks,
  queue/admission metrics, RSS/CPU/threads, Redis counters and local alert
  transitions. Prometheus scrape and alert-rule examples are included.
- **Load testing:** `scripts/load_test.py` starts its own authenticated HTTP server
  with a temporary conversation database, uses real models and the active catalog,
  samples memory, exercises concurrent shopping turns, retries, ownership and
  overload, and stops only its own API process.

See [operating instructions](../docs/operations.md) for provisioning, migration,
API retry semantics, configuration and commands.

## Automated verification

**291 tests passed**, including the PostgreSQL and Redis integration tests, in
12.37 seconds. The single warning is the existing Starlette/httpx TestClient
deprecation. No tests were skipped in this integration-enabled run.

New tests cover ownership on every conversation route, admin separation, legacy
privacy, missing/revoked tokens, duplicate creation, message replay across restart,
changed-body conflicts, in-flight duplicates, failure/retry, rate limits, queue
full/timeout behavior, conversation ordering with two model slots, transactional
rollback, cancelled blocking work, second-worker refusal, abandoned claim cleanup,
commerce version stability, alerts, oversized bodies and OpenAPI authentication.

A restricted-sandbox rerun could not reach PostgreSQL/Redis sockets. The final
run above used permitted local-service access and passed; those socket failures
were environmental, not counted as successful tests.

## Real HTTP/model load measurement

Measured with the existing 50,000-product catalog, `ministral-3:8b`,
`embeddinggemma:latest`, and the local reranker running on MPS.

Active search generation:
`a8b2a266655f6315595355f23dbc667d8aef93ec6f92c350bfb61fcb60350180`.

Command:

```sh
.venv/bin/python scripts/load_test.py --output data/operations/load_2026-10-20.json --queue-size 4
```

The test used one model slot, four waiting slots, and high per-user rate budgets
so the normal workload measured model/queue capacity rather than rate rejection.
Production defaults remain one model slot, eight waiting slots, HTTP 120/minute
with burst 30, and model workloads 30/minute with burst 8. Unit tests separately
verified 429 behavior. Each shopper asked for products and then cheaper options;
categories included moisturizers, handbags and lipstick.

| Concurrent conversations | Shopping turns | HTTP 200 | Median turn latency | p95 turn latency | Completed turns/sec |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 2 | 10.71 s | 18.84 s | 0.093 |
| 2 | 4 | 4 | 6.72 s | 13.42 s | 0.278 |
| 4 | 8 | 8 | 12.37 s | 24.69 s | 0.322 |

**All 14 shopping turns returned recommendations with five product cards.** This
is operational success, not a new human review of recommendation relevance.
Small-sample p95 values here equal the maximum. Later phases benefit from warm
models/cache; the lower latency at concurrency 2 does not establish that adding
concurrency makes individual uncached inference faster.

- Startup including embedding/reranking warm-up: **5.73 seconds**. Chat-model
  loading remains part of the first real planner request.
- Redis in a fresh isolated namespace: **8 hits / 6 misses = 57.1% hit rate**.
  No cache unavailable/write-failure counts occurred. The workload deliberately
  includes repeated popular requests; this is not a projected production hit rate.
- A completed retry returned the same message ID. A foreign owner was denied.
  An in-flight duplicate/replay left exactly one user/assistant pair.
- Deliberate overload submitted seven simultaneous chat turns with five total
  admitted slots: **five completed, two returned expected 503 responses**.
  Successful overload p95 was **21.51 seconds**. These rejections are reported
  separately from the normal workload, not hidden as successful turns.
- Peak sampled API RSS: **738.6 MiB**. Minimum available system memory:
  **3.68 GiB**. There were 182 samples at a nominal 0.5-second interval.
  Shared Ollama process RSS peaked at about **60.1 MiB**; that number excludes
  substantial GPU/unified-memory allocations and is **not total model memory**.
- Entire run: **95.11 seconds**. All required dependencies and Redis were ready
  at the final probe; there were no active alerts at that instant.

Raw observations: [JSON](../data/operations/load_2026-10-20.json),
[Prometheus snapshot](../data/operations/load_2026-10-20.prom),
[server log](../data/operations/load_2026-10-20.server.log).
These generated files are local and ignored by Git. The measured model/routing,
admission and deduplication implementation is unchanged by subsequent documentation,
Swagger schema and disabled-dependency alert-rule adjustments; those adjustments
are covered by the final automated suite.

## Local setup and practical boundaries

`local-user` and `local-admin` identities were provisioned. Their secrets are in
mode-0600 files under ignored `data/credentials/`; no plaintext secret is stored
in tracked code or this report. `.env` enables authentication and request keys,
and points the terminal client at `data/credentials/local-user.token`.

**Eight existing conversations remain unowned and preserved.** Authenticated
clients cannot read them until the administrator deliberately assigns them with
`manage_api_users.py adopt-legacy --name local-user`, if they belong to that user.
No existing conversations were reassigned or deleted.

This is a short local capacity measurement, not a sustained production soak test
or an SLO guarantee. Limits/metrics are process-local; use one worker. Remote
serving still needs deployment-specific HTTPS and protected host access. Local
alerts and Prometheus rule examples are implemented; an external Prometheus/
Alertmanager service and email/Slack delivery were not deployed. No notification
was sent. In-process monitoring cannot report its own death without the external
scraper's API-down rule. Request records are durable without automatic retention
expiry. Model inference interrupted before commit can be repeated after a crash;
committed turns are deduplicated. Backups remain outside the requested scope.

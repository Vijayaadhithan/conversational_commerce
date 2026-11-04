# Local hardening and demo readiness

The backend retains its single-worker SQLite contract, successful response shapes,
mandatory search filters, provider selection, and embedding contract. This work
adds security and resource controls; it does not deploy Docker, run ETL, rebuild
embeddings, or establish private multi-customer catalog isolation.

## Behavior changes

- CLI-issued tokens expire after seven days by default. Existing tokens retain
  their existing lifetime and full scope until explicitly rotated or revoked.
- New CLI user tokens default to `shopping`; admin tokens default to `*`.
  `rerank` permits the direct `/rerank` endpoint. `ops` also requires the admin
  role. Shopping searches may still use internal reranking.
- Deleting a conversation erases its replay payloads in the same transaction.
  On upgrade, replay copies of previously deleted conversations are erased too.
- Deleted/expired retry records return **410**. A small tombstone preserves the
  owner, operation, key, request hash, timestamp, and conversation ID so the same
  key cannot accidentally execute again. Payload retention is explicit, not an
  automatic background purge. Logical deletion does not promise forensic erasure
  from SQLite pages, WAL files, or old backups.
- Internal catalog errors now return a generic 503. Use protected operational
  status and catalog verification tools for diagnosis.
- A timed-out turn returns 504 with same-key recovery instructions. Work already
  running keeps its admission slot and retry claim until it drains. Deadline
  checks prevent starting subsequent model steps or entering a late commit.
  A transaction already entered may finish; retrying the same key recovers it.

## Token administration

```sh
.venv/bin/python scripts/manage_api_users.py issue --name demo-user \
  --expires-days 7 --label demo --scopes shopping \
  --token-file data/credentials/demo-user.token
.venv/bin/python scripts/manage_api_users.py tokens
.venv/bin/python scripts/manage_api_users.py revoke --token-id TOKEN_ID
```

`tokens` prints identifiers, labels, expiry, scopes and revocation status, never
bearer tokens or token hashes. Issue a new token first, switch the client, then
revoke the old token ID. `revoke --name NAME` still revokes all tokens for a user.
Do not embed shared service/admin credentials in browser bundles.

## Resource and failure controls

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

## Paid API budgets

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

## Configuration, dependencies and release checks

`DEPLOYMENT_PROFILE=demo` validates authentication, idempotency, hosted generation
and reranking, bounded admission, call caps and a suitable RSS alert threshold at
startup. Embedding placement is deliberately unchanged. Inspect configuration
without starting services with `scripts/check_deployment.py`. This does not prove
live access, resource capacity, TLS setup, or private tenant isolation.

`requirements.lock` pins runtime versions from the local baseline, plus the
certifi advisory fix. Install it directly for a demo runtime. Linux CI installs
development requirements constrained by this lock and runs `pip check` and tests.
The lock pins versions; it is not an artifact-hash lock. Dependency PRs must update
the lock and pass the Linux checks before deployment.

Security CI uses pip-audit for known dependency advisories and Gitleaks with
redacted output for repository history. Both are blocking jobs, and a weekly
schedule checks for newly disclosed issues. Dependency and GitHub Actions version
updates are manual; no Dependabot update configuration is included. These checks do not establish the absence of all security
vulnerabilities. Sources: [pip-audit](https://github.com/pypa/pip-audit) and
[Gitleaks](https://github.com/gitleaks/gitleaks).

Both workflows run on pushes to `main`, pull requests, and manual dispatch.
Feature-branch pushes without a PR do not run CI automatically. A merged PR
still triggers checks on the resulting `main` commit. Newer runs cancel older
unfinished runs of the same workflow for the same branch/PR; completed runs remain
in history. The quality workflow reuses matching pip caches, but each run executes
its jobs again. Neither workflow uploads build artifacts. The weekly security
schedule remains enabled. Runner time already consumed is not refunded by
cancelling or deleting a run. These controls reduce wasted usage but do not set
account-level spending limits.

Before private customer catalogs or multiple API replicas, implement tenant scope
across catalog queries, Qdrant, caches, administration, and shared coordination.
Storefront categories and conversation ownership do not replace tenant isolation.

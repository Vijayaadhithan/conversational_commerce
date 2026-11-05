# Single-server Docker deployment

This deployment targets Linux with 2 vCPUs and 8 GiB RAM. PostgreSQL, Qdrant,
and Ollama run continuously; the API runs under the `app` Compose profile.
Only the existing EmbeddingGemma weights are installed. Generation uses Groq
`openai/gpt-oss-20b`, and reranking uses Voyage `rerank-2.5`. The selected provider
keys are configured in the server's private `.env.server`; no provider keys are
copied into the frontend or committed to the repository.

Layout on the demo server:

- `/opt/commerce/conversational_commerce`: backend source, `.env.server`, data.
- `/opt/commerce/conversational_commerce_etl`: ETL source and existing data;
  no automatic ETL or enrichment jobs.
- `/opt/commerce/state`: persistent PostgreSQL, Qdrant and Ollama directories.
- `/opt/commerce/secrets/postgres_password`: private generated database password.
- `/opt/commerce/backups`: the initial consistent transfer/recovery bundle.

The backend image contains source and locked Python dependencies, with no local
environment, tokens, catalog, downloaded models or database baked into it.
The API/tools run as UID 10001 with a read-only root filesystem. Runtime data
must be writable by UID 10001. Dependency image digests are recorded in
`deploy/images.env` and selected by `scripts/server_compose.sh`.

The API/tools use Linux host networking and connect to loopback dependency ports.
PostgreSQL, Qdrant and Ollama publish only on `127.0.0.1`. This preserves the
Qdrant client's existing remote-HTTPS requirement without weakening validation.
The API binds only to `127.0.0.1:8000`. The `public` Compose profile runs Caddy
on ports 80 and 443 for `commerce-api.querix.co`, with a publicly trusted
certificate, automatic renewal and HTTP-to-HTTPS redirects. Certificate state
persists in `/opt/commerce/state/caddy-data`. Caddy proxies SSE without response
buffering and blocks operations/documentation routes at the public edge.
Authenticated operations remain available through loopback/SSH. Host networking
is a single-host tradeoff, not container-level network isolation for the API.

## Inspect and configure

```sh
cd /opt/commerce/conversational_commerce
sh scripts/server_compose.sh ps
nano .env.server
```

Set `LLM_PROVIDER` to `google`, `groq`, `openrouter`, or `nvidia`, set `LLM_MODEL` to the
exact model ID, and fill the matching `GOOGLE_API_KEY`, `GROQ_API_KEY`, or
`OPENROUTER_API_KEY`, or `NVIDIA_API_KEY`. Set `VOYAGE_API_KEY` for the configured Voyage reranker.
Keep `EMBEDDING_PROVIDER=ollama` and the existing model name and digest; changing
embedding identity requires rebuilding the catalog. Do not copy the production
example over `.env.server`: the server file includes its database connection.

The environment file is private (0600), excluded from Git and the Docker build.
Do not print `docker compose config` without `--quiet`: it expands credentials.

## Validate and start

```sh
cd /opt/commerce/conversational_commerce
sh scripts/server_compose.sh run --rm --no-deps tools python scripts/check_deployment.py
sh scripts/server_compose.sh --profile app up -d api
curl --fail http://127.0.0.1:8000/livez
```

Configuration checks do not contact paid models. `/livez` checks process liveness;
`/ready` requires an API bearer token and checks dependency
availability; it does not prove paid quota or full conversation behavior.
Adding/changing `.env.server` requires recreating the API container, using the
`up -d api` command above rather than just `restart`.

Access locally without exposing an unencrypted public API:

```sh
# In the local conversational_commerce checkout; keep this terminal open.
sh scripts/connect_demo_server.sh

# In another local terminal:
cd /Users/vjaadhi2799/Downloads/querixco
npm run dev -- --host 127.0.0.1 --port 5173
```

Use the existing private API token with the local chat client. Authentication and
conversation ownership remain enforced; ownerless conversations are not reassigned.
Use one API worker per SQLite database. Defaults allow one active model workload
and a queue of four. Redis is disabled; the application supports operating without it.

Querix's existing ignored `.dev.vars` points to `http://127.0.0.1:8000` and holds
its dedicated backend token and session-signing secret. These values remain
server-side in the local Worker gateway. Open `http://127.0.0.1:5173/beauty`,
`/clothing`, `/footwear`, `/fashion`, or `/demo/commerce` to use the remote API
through the tunnel. Stop any local backend occupying port 8000 before opening
the tunnel; do not run two backends on that port.

The hosted origin is `https://commerce-api.querix.co`. Its DNS A record points
directly to the server (DNS only). The existing `api.querix.co` Personal RAG/analytics
origin is separate and must not be repointed. Start the public proxy with:

```sh
sh scripts/server_compose.sh --profile public up -d proxy
curl --fail https://commerce-api.querix.co/livez
```

Public readiness and catalog routes require the same bearer authentication as
loopback access. `/ops/*`, `/docs`, `/redoc` and `/openapi.json` return 404 publicly.

The `querixco` Worker requires three runtime secrets: `COMMERCE_API_URL` set to
the HTTPS origin, `COMMERCE_API_TOKEN` set to its dedicated non-admin backend token,
and `COMMERCE_SESSION_SECRET` set to a random signing secret. Provider keys remain
only on the backend. Local `.dev.vars` is not uploaded as production configuration.
Deploy checked frontend code with Wrangler; preserve unrelated existing bindings.

The production frontend is deployed at `https://querix.co` and `https://www.querix.co`.
Worker version `26d1dcb0-8f1e-40cd-8a91-fa6fbae8471b` was deployed with all three
commerce bindings as encrypted secrets. Public checks passed for all four shopping
pages, a hosted shopping response with five products, SSE completion, saved history,
idempotent replay and section isolation. The production frontend does not need the
local SSH tunnel. The frontend's full check passed (40 tests plus type/lint/dead-code
checks and the production build).

Caddy configuration is in `deploy/Caddyfile`; its image digest is recorded in the
server's `deploy/images.env`. To update the proxy configuration with its admin API
disabled, validate it using a one-off `proxy caddy validate` container, then restart
the `proxy` service. Preserve its certificate-state directories during updates.

The local frontend was verified against the server through SSH: catalog access,
hosted shopping responses, saved history, idempotent replay, SSE completion and
isolation across all four shopping sections passed. Only disposable test
conversations were created/deleted. The original 23 conversations and 94 messages,
including eight ownerless conversations, were preserved. These checks establish
integration behavior, not sustained load capacity or comprehensive answer quality.

The frontend's `/api/commerce/status` reports whether its bindings are configured;
it does not itself prove upstream availability. If live mode reports a connection
error, check the SSH tunnel and authenticated backend readiness. Keep the tunnel
terminal open; the helper intentionally uses its own persistent SSH connection.

## Restore and operate

The initial recovery bundle contains SQLite, a PostgreSQL custom-format dump,
Parquet embeddings and a checksum manifest. Restore into an empty PostgreSQL
database, restore SQLite and embedding files, select a new Qdrant collection
prefix, then run `scripts/build_qdrant_index.py` through the tools container.
Index reconstruction uses saved vectors and makes no embedding/paid-model calls.
See `docs/local_hardening.md` for the complete recovery procedure.

The transfer archive and recovery bundle are on this same server; they are not
off-server backups. Automated off-server backup scheduling and external alert
delivery remain separate work. Container logs are bounded, data
persists across container recreation, and infrastructure restarts automatically.
Do not run `down -v`, remove state directories or overwrite a populated database
as part of ordinary application updates.

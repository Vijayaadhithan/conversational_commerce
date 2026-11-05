# Local and hosted model providers

Choose chat, embeddings, and reranking independently. The default `.env` and
`.env.example` keep Ollama and the local reranker enabled. `.env.production.example`
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
To switch chat to GroqCloud or OpenRouter, change `LLM_PROVIDER` and `LLM_MODEL`,
and provide that provider's credential. Google embeddings and Voyage reranking
continue using their own credentials. Select a chat model supporting system
messages, temperature, and JSON object output. Provider-specific reasoning token
budgets may require a larger `LLM_MAX_TOKENS`; truncated output is rejected.

`LLM_CONTEXT_SIZE` controls the app's conservative prompt budget; set it within
the selected model's capacity. In Ollama mode it also configures `num_ctx`.
Blank `LLM_*` limits inherit their corresponding existing `OLLAMA_*` values.
Restart the API after configuration changes. There is no automatic fallback
between providers. Invalid shopping intent gets at most one repair call; transport,
authentication and rate-limit errors do not trigger that repair. Optional commerce
summaries add one bounded call through the same configured provider. Ollama uses
NDJSON streaming; hosted adapters consume compatible SSE. Provider failure falls
back to the deterministic product reply without switching providers.

## Embedding migration

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

## Credentials and serving security

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
and run one worker per conversation SQLite database; see [operations](operations.md).
Provider API keys are separate from tokens used by clients of this application.

Queries/catalog excerpts go to the selected embedding/reranking provider;
conversation context goes to the selected LLM provider. This is an explicit
change in data destination when hosted mode is enabled.

## Readiness and validation

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

## NVIDIA and CPU outage fallback

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

# NVIDIA production verification — 2026-11-05

The commerce API now uses `nvidia/nemotron-3-ultra-550b-a55b` through NVIDIA's
hosted endpoint, with thinking disabled. EmbeddingGemma remains local in Ollama;
the embedding index and 50,000-product catalog were not rebuilt.

Both supplied NVIDIA candidates were tested using the application's actual
structured planner for a constrained product request, a refusal of recommendations,
and two separate product requests, plus one grounded reply. Both completed all
four checks correctly. With the same 32,768 context budget, 2,048 maximum output
tokens and temperature zero, observed per-call timing was:

| Candidate | Mean | Range |
| --- | ---: | ---: |
| google/gemma-4-31b-it | 28.50 s | 18.76–39.35 s |
| nvidia/nemotron-3-ultra-550b-a55b | 3.33 s | 2.49–3.90 s |

This is a small live sample, not a concurrency or general model-quality benchmark.
Nemotron was selected for the much lower observed latency at comparable correctness.
Private raw results are in ignored `data/nvidia_comparison.json`; no keys are recorded.

Production and local `.env` share the selected provider/model/credential,
32,768 context budget, 2,048 output cap, 120-second provider timeout and Voyage
configuration. Their database, authentication, embedding and machine-specific
operational settings are preserved. The larger bounded context allows product
reply generation to receive more evidence; evidence validation and review can
still select the existing deterministic reply when model prose is rejected.

Voyage `rerank-2.5` remains primary, with a 12-second provider timeout. On failure,
the pinned MiniLM ONNX CPU model ranks the same eligible candidates offline.
Application spending caps and expired request deadlines are not bypassed.
Four real CPU inference checks passed inside the server image in 1.08 seconds
at 269.3 MiB process RSS. A complete isolated shopping flow with intentionally
invalid Voyage credentials returned five budget-compliant products, used local
ranking, and preserved history/replay; the canary container used about 317 MiB.
The API retains its 1 GiB memory limit and one admitted model workload.

The public `https://querix.co` flow passed all four storefront/session-isolation
checks. Its shopping SSE turn returned five products in 9.60 seconds, persisted
history, and replayed the same message ID without duplicating a turn. The API at
`https://commerce-api.querix.co` passed authenticated readiness. Temporary test
conversations were removed. All 23 original conversations and 94 messages match
the initial snapshot; eight ownerless conversations retain their original owners.

Live Docker services: commerce API, PostgreSQL, Qdrant, Ollama embeddings and
Caddy HTTPS. The frontend remains on Cloudflare. ETL source and approximately
1.9 GiB of data exist at `/opt/commerce/conversational_commerce_etl`; there is no
ETL virtual environment, running job, cron entry or systemd schedule. The API
serves the previously imported catalog; this deployment does not start new ETL.

Rollback: the prior image is `commerce-api:pre-nvidia-20261105`; its private
configuration and source backup are under
`/opt/commerce/backups/pre-nvidia-20261105`. Restore that configuration, tag the
prior image as `commerce-api:demo`, and run
`sh scripts/server_compose.sh --profile app up -d --no-deps --force-recreate --timeout 210 api`.
Do not restore databases merely to revert a provider change. The Compose API
now allows 210 seconds for graceful shutdown of admitted work.

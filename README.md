# Conversational Commerce Backend

A local FastAPI backend for persistent conversations with Ollama, plus a local
1.54B-parameter reranker and scripts for downloading fashion/beauty metadata.

- Chat model: `ministral-3:8b` through Ollama.
- Conversation memory: SQLite transcripts, recent turns, and rolling summaries.
- Reranker: [Mixedbread mxbai-rerank-large-v2](https://huggingface.co/mixedbread-ai/mxbai-rerank-large-v2),
  running through PyTorch on Apple MPS or CPU.
- Catalog inputs: All Beauty, Amazon Fashion, and Beauty & Personal Care.

Automatic catalog retrieval and vector indexing are not implemented yet.
`POST /rerank` scores documents supplied by the caller.

## Setup

Use Python 3.12 or later. Apple Silicon is recommended for local GPU reranking.

```bash
git clone https://github.com/Vijayaadhithan/conversational_commerce.git
cd conversational_commerce
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` for local settings. It is ignored by Git; `.env.example` contains
only shareable defaults. No hosted API key is required. Keep an existing `.env`
when updating the project rather than copying over it.

Install and start [Ollama](https://ollama.com/), then download the chat model:

```bash
ollama pull ministral-3:8b
```

If the Ollama desktop app is not running, start its server in another terminal:

```bash
ollama serve
```

## Run the backend

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use one Uvicorn worker. Interactive API docs: http://127.0.0.1:8000/docs.
Health check: http://127.0.0.1:8000/health.

In another terminal, start a continuous conversation:

```bash
.venv/bin/python scripts/chat.py
```

The client prints a conversation ID. `/history` shows the transcript, `/new`
starts another conversation, and `/exit` quits. Resume a saved chat with:

```bash
.venv/bin/python scripts/chat.py --session YOUR_CONVERSATION_ID
```

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
downloaded. Records still need normalization, deduplication, and indexing.
Do not treat historical prices or availability as current commerce facts.
Check source terms before redistributing third-party data or weights.

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

Messages accept up to 4,000 UTF-8 bytes. Responses are returned after generation,
without token streaming. If `done_reason` is `length`, the output limit was
reached; increase `OLLAMA_MAX_TOKENS` or ask the model to continue.

Full conversation transcripts persist across restarts. Older turns are
summarized to keep model input bounded; summaries are lossy. Failed inference
does not save a partial turn or partial memory update.

This backend binds to localhost and has no authentication. Model weights,
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

# Local Querix commerce demo verification — 2026-10-23

The sibling `querixco` frontend is connected to this backend at
`http://127.0.0.1:5173/demo/commerce`. This change covers the local backend
connection and shopping discovery journeys. Public hosting and checkout are
outside this implementation.

## Catalog and connection

- The active PostgreSQL catalog and search generation contain 50,000 products
  across 14 beauty and fashion categories. Live searches use this complete
  generation, with a ranked selection returned each turn.
- Older imports are archived versions, not additional inventory to combine.
- `GET /catalog/summary` is authenticated and reads coverage from a consistent,
  read-only snapshot of the active catalog/search generation.
- The frontend uses a dedicated non-admin demo identity, server-only bindings,
  and a signed HttpOnly browser-session cookie. Secrets are in ignored files
  with owner-only permissions. No `.env` model settings were changed.
- Live mode is the default when configured. Sample mode remains optional and
  visibly labelled. Missing live facts remain unknown; sample facts are not
  merged into live products. Prices remain historical USD listing prices.

## Verification results

`scripts/check_frontend_live.py` passed using the actual browser-facing gateway,
local model, embedding/reranking stack, and active catalog. It used independent
test conversations and deleted only those conversations when complete.

| Journey | Result |
| --- | --- |
| Dry-skin concern, then moisturizer request | Clarification followed by five products |
| Budget change from under $30 to under $20 | Five products priced below $20 |
| Retry identical request key | Same response ID; no duplicate history entries |
| Compare first two in task 1 | Two previously displayed products |
| Details by displayed product ID | Correct single product |
| Reconnect and restore | History and product metadata retained |
| More options for task 1 | Five products returned |
| Moisturizer under $0.01 | No matching products; requirements preserved |
| Shampoo under $15 and moisturizer under $25 | Two separate groups, ten products |
| Change shampoo budget to under $10 | Moisturizer budget remains $24.99 |
| Independent browser session | Empty history, isolated from the first session |
| Delete and start fresh | Old session rejected; a new conversation can start |
| Cotton shirt for adult men under $40 | Five products below $40 |

The successful run's recommendation turns took about 3–17 seconds on this
machine. Comparison, details, and duplicate replay took about 0.01 seconds.
These are local observations, not a general latency guarantee. The raw report
is ignored at `data/frontend/live_report.json`.

Safari verification additionally confirmed automatic live mode, the 50,000-item
coverage panel, real product images, comparison attributes, product details,
and full conversation restoration after page refresh. The Cheaper options
button reduced the budget to $9.98 and returned five products within it.

Automated checks:

- Backend focused tests: **135 passed** across catalog summary, search API,
  commerce flow, and conversation regression tests.
- Frontend `npm run check`: passed TypeScript, lint, **27 tests**, unused-code
  checks, and production build.
- Ruff checks passed on the changed backend code and scripts.

Two integration defects were repaired: the Worker transport now uses supported
manual redirect handling and rejects redirects; explicit task-scoped UI actions
now route deterministically instead of depending on model interpretation.
Free-form shopping messages continue through the conversational planner.

## Run locally

Configure once from this backend checkout:

```sh
.venv/bin/python scripts/setup_local_frontend.py
```

Start the backend and frontend in separate terminals:

```sh
# conversational_commerce
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```sh
# ../querixco
npm run dev -- --host 127.0.0.1 --port 5173
```

Keep the existing PostgreSQL, Redis, and Ollama services available. Wait for
backend startup to complete before opening the demo. Use `127.0.0.1`
consistently, because `localhost` has a separate browser cookie.

Repeat the integration check while both servers are running:

```sh
.venv/bin/python scripts/check_frontend_live.py
```

Broader model/relevance evaluation remains separate from these verified
journeys. This result does not claim every possible natural-language request
has been validated. No public deployment was performed.

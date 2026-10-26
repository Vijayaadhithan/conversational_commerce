# Embedding lifecycle

This extends the versioned catalog/import and search-generation design. Run
`scripts/init_catalog.py` to apply the catalog and Parquet-reference schema. Catalog
snapshots remain authoritative: update the ETL output, import the completed
manifest, then synchronize the search build.

## Incremental synchronization

```bash
.venv/bin/python scripts/import_catalog.py
.venv/bin/python scripts/build_search_index.py
.venv/bin/python scripts/verify_search_index.py
```

| Change | Embedding work | Index/search behavior |
| --- | --- | --- |
| Identical active build | None | Verify and reuse the serving generation |
| New product | Embed unless an identical compatible input is already available | Add to the next generation |
| Changed selected embedding text | Embed unless the new input already exists in a compatible ready generation | Replace through generation publication |
| Price, provenance, or other metadata only | Reuse the compatible vector | Regenerate the current metadata and keyword row |
| Change only beyond the embedding text budget | Reuse the vector | Regenerate current bounded keyword text |
| Model digest or input contract changes | Do not reuse incompatible vectors | Build a complete compatible generation |
| Product absent from new catalog snapshot | No embedding | Exclude from the next active generation |
| Empty catalog snapshot | No embedding | Publish zero active products |

Reuse requires the exact selected-input SHA-256, model digest, and full input
contract, including dimensions and prompt/text-producer hash. Only retained ready
generations supply cached vectors. An alias for identical model weights may
reuse vectors, but the new build records the configured model name. A B-tree
lookup index supports cache matching. Reuse copies the vector into the new
generation's Parquet batch; it does not depend on the old generation's files or rows.

The keyword title/body and source references come from the **current** catalog
record, never from the cached row. `embedding_reused` records whether a row's
vector was copied. Command results distinguish `embedded_this_run`,
`copied_this_run`, and total `build_reused_vectors`. Counts refer to product rows,
not necessarily distinct input strings.

Incremental refers to model work. Each published generation still stores a
complete snapshot of SQL metadata and Parquet vectors. This uses extra disk while old
generations are retained; it is not in-place mutation of the serving index.
The full source product is preserved even when the embedding/keyword text is
bounded. See [search.md](search.md) for those text limits.

## Re-embedding existing products

```bash
# Replace these example IDs with IDs from the active catalog.
.venv/bin/python scripts/build_search_index.py \
  --reembed-product amazon:YOURASIN01 \
  --reembed-product amazon:YOURASIN02

.venv/bin/python scripts/build_search_index.py --reembed-all
```

Selective refresh forces inference for the requested IDs; other rows can reuse
compatible vectors. Missing IDs fail before creating the refresh. Full refresh
forces inference for every current product, even if the selected text is
unchanged. Both use a distinct generation ID and leave the previous serving
generation untouched until verification and publication succeed.

Every forced invocation starts a new refresh. The script prints its build ID
before processing. Resume that same refresh after an interruption:

```bash
.venv/bin/python scripts/build_search_index.py --resume-build BUILD_ID
```

Committed batches are retained; only missing rows are processed. Resume requires
the same active catalog, model identity, and input contract. It cannot be combined
with force flags. Normal synchronization preserves an already active compatible
refreshed generation instead of reverting to an older default build. Explicitly
resuming a ready generation verifies it and activates it when still compatible.

## Product removals and physical cleanup

Remove a product from the next authoritative ETL subset and import/synchronize
that snapshot. The product cannot appear in that generation's keyword or vector
results. After catalog activation, search refuses an old mismatched build until
the new generation is ready. An empty subset with manifest count/size zero is
supported, with a positive requested subset size retained in its manifest.

Old source/catalog snapshots and inactive search generations are retained for
provenance/rollback. Vector retention is explicit and scoped to this database:

```bash
# Dry run: keep the active build and one newest inactive ready build for rollback
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 1

# Execute the policy after reviewing the displayed IDs/counts
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 1 --remove-qdrant --apply

# Keep no inactive ready generations; unfinished builds are still preserved
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 0 --remove-qdrant --apply

# Also discard unfinished builds when intentionally abandoning their resume state
.venv/bin/python scripts/manage_search_index.py prune --keep-ready 0 --include-incomplete --remove-qdrant --apply

# Preview deletion of explicitly selected inactive generations; repeat --build-id as needed
.venv/bin/python scripts/manage_search_index.py delete --build-id BUILD_ID
.venv/bin/python scripts/manage_search_index.py delete --build-id BUILD_ID --remove-qdrant --apply
```

Both removal commands default to dry run. Plans list exact build IDs, vector row
counts, and preservation reasons. Active builds are always protected, even when
explicitly requested. Unknown IDs or any requested active ID fail before deletion.
Apply recomputes the policy under locks; inspect that invocation's returned plan
as well if database state changed since the preview. The tools never delete
catalog/source records, model weights, raw data, or another project's database.

Deletion removes inactive search-document metadata and its build metadata in
one SQL transaction. Parquet files are removed separately after that commit. It coordinates with builds,
catalog imports, and migrations. Stop API readers before deleting Qdrant collections; a pinned PostgreSQL snapshot
cannot preserve a deleted remote collection. See [retention](operations.md#retention-policy)
for alias checks, explicit remote cleanup, and unreferenced catalog-import pruning.

After deleting builds, preview and remove their unreferenced Parquet directories:

```bash
.venv/bin/python scripts/manage_search_index.py prune-artifacts
.venv/bin/python scripts/manage_search_index.py prune-artifacts --apply
```

This protects every registered build, including unfinished builds. Failed SQL batches
may leave unreferenced files inside a registered build; those remain until the entire
build is retired. Deletion is retryable and runs under the build/import locks. Never
share `SEARCH_ARTIFACT_DIR` across databases. Back up this directory with the matching
SQL catalog and build metadata; Qdrant alone is not the embedding snapshot backup.

## Index maintenance

Normal inserts/deletes maintain metadata lookup/B-tree indexes through PostgreSQL.
Qdrant owns online dense/BM25 indexes; this command maintains SQL catalog/build metadata.

```bash
.venv/bin/python scripts/manage_search_index.py reindex
.venv/bin/python scripts/manage_search_index.py vacuum
```

`reindex` runs `REINDEX TABLE CONCURRENTLY` for `commerce.search_documents` and
`commerce.catalog_products`, refreshes planner statistics, and checks index
validity. It does not generate vectors or change model/input versions. It uses
the concurrent form to allow ordinary database access while rebuilding indexes;
extra I/O and brief locking can still affect latency. See
[PostgreSQL REINDEX](https://www.postgresql.org/docs/16/sql-reindex.html).

`vacuum` runs ordinary `VACUUM (ANALYZE)` for search documents/builds after
retention. Deleted storage becomes reusable; the database file may not shrink
on the filesystem. This is not `VACUUM FULL`. See
[PostgreSQL VACUUM](https://www.postgresql.org/docs/16/sql-vacuum.html).

Maintenance fails promptly if another build/import/migration holds the relevant
locks. Concurrent reindexing cannot run inside a transaction; an interrupted
operation can leave an invalid transient index. The command reports invalid
indexes rather than deleting arbitrary database objects automatically; inspect
the PostgreSQL error and index state before retrying.

## Validation

The PostgreSQL integration suite verifies incremental additions/changes,
price-only and keyword-only updates, product removal including all products,
model/contract invalidation, selective/full refresh, failed-refresh resume,
preservation of the active refresh, protected preview/deletion/retention,
concurrent reader snapshots, index rebuilds, and ordinary vacuum. Tests use
separate randomly named databases and remove them after execution.

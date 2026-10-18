# PostgreSQL catalog import

The recommendation catalog belongs to this backend. The separate
[ETL repository](https://github.com/Vijayaadhithan/conversational_commerce_etl)
owns raw-data normalization and reproducible sampling. This importer consumes
its completed `subset.jsonl`, not the full raw archives or full normalized
catalog. Conversation transcripts and summaries continue to use SQLite.

## Configuration and commands

```dotenv
CATALOG_DATABASE_URL=postgresql:///conversational_commerce
CATALOG_ETL_OUTPUT_DIR=../conversational_commerce_etl/data/processed
```

Store database credentials only in the ignored `.env`. CLI diagnostics never
print the connection string or the raw PostgreSQL exception. Relative ETL paths
are resolved against the backend project root, independent of shell directory.

```bash
.venv/bin/python scripts/init_catalog.py --create-database
.venv/bin/python scripts/import_catalog.py
.venv/bin/python scripts/verify_catalog.py
```

Initialization creates the named database only when missing, then applies SQL
migrations. It refuses the PostgreSQL administrative/template databases. It
does not alter roles, server configuration, other databases, Redis, models, or
SQLite. Database creation needs CREATEDB permission; migrations need schema and
table creation privileges in the chosen database. `CATALOG_ADMIN_DATABASE_URL`
may select a separate administrative connection for database creation.

Migration files in `migrations/catalog/` are ordered and checksummed. Never edit
an applied migration; add a new migration. Applied-file checksum mismatches fail.
The migration ledger and import writer lock are scoped to the catalog database.

## Tables and active view

| Object | Purpose |
| --- | --- |
| `commerce.schema_migrations` | Applied SQL migration names and SHA-256 checksums |
| `commerce.catalog_imports` | Original manifest, run/schema/subset fingerprints, record count, import time |
| `commerce.catalog_products` | Immutable snapshot records, product IDs, original line hashes, row ordinals, generated typed projections |
| `commerce.catalog_active` | One active import ID and activation timestamp |
| `commerce.active_products` | Products belonging to the active snapshot only |

An import ID is the SHA-256 of the manifest bytes. Products are unique by
`(import_id, product_id)`. Their complete normalized `document` JSONB retains
category derivation evidence, explicit listing claims and field references,
content, images, unknown availability/attributes, and all observation provenance.
Object-key order and JSON whitespace are not retained by JSONB; the original
subset-line SHA-256 is retained separately.

`product_id`, `parent_asin`, `title`, `domain`, `family`, `brand`,
`historical_price`, `currency`, `attributes`, and `source_name` are generated
columns, so they cannot disagree with their JSONB source. Historical prices use
PostgreSQL `numeric`, without rounding into a binary float or a small fixed
precision. Category/price and case-insensitive brand B-tree indexes are included.
Embedding columns and vector indexes are not created in this phase.

The importer owns writes to these tables. Ordinary PostgreSQL owner privileges
can still modify them; independent verification detects drift. This local
development setup does not provision separate production reader/writer roles.

## Integrity and failure behavior

The default input is the run selected by ETL `current.json`. The importer pins
that manifest and an open subset file descriptor for its entire operation.
Preflight streams every row, checking:

- Local schema bytes match the manifest's schema SHA-256 and supported version.
- Subset bytes, SHA-256, and product count match the manifest.
- Every record satisfies the pinned JSON Schema and has a unique consistent ID.
- Eligibility includes a known domain/family, positive historical USD price,
  descriptive content, and no critical cross-observation conflicts.
- Exactly one selected observation matches the record's selected provenance;
  source names and row numbers reference the manifest's declared inputs.
- Content and content-source arrays agree in length. Unknown values remain null.
- Duplicate JSON keys, non-finite numbers, NUL, invalid Unicode, and oversized
  records fail explicitly instead of being silently repaired or truncated.

The schema is vendored unchanged from ETL contract v1.0.0 in
`schema/catalog-product-v1.schema.json`; compare its bytes with the ETL contract
when intentionally upgrading versions. The ETL manifest is a local integrity
record, not a signed or independently authenticated source. Import validation
checks the **subset and schema**; it does not rehash raw archives or re-scan the
full catalog. Use the ETL project's `scripts/verify_outputs.py` for that audit.

The database transaction obtains an advisory writer lock, COPYs into a temporary
staging table, rechecks the subset hash, inserts snapshot metadata/products, and
compares every stored record and generated projection to the source. Only then
does it switch the active pointer. All mutations roll back together on failure.
The temporary table is dropped automatically. A second concurrent importer
fails promptly and can be retried; readers continue to see committed snapshots.
Bulk transfer uses [Psycopg's row-oriented COPY API](https://www.psycopg.org/psycopg3/docs/basic/copy.html)
and [PostgreSQL COPY FROM STDIN](https://www.postgresql.org/docs/16/sql-copy.html),
so the PostgreSQL server does not need direct access to ETL files.

Reimporting an existing manifest verifies its records before activating it.
There is no stale-record accumulation in the active view. Previous snapshots
are retained on disk and require a future explicit retention policy as imports
grow. To select an older completed run deliberately:

```bash
.venv/bin/python scripts/import_catalog.py --manifest /path/to/run/manifest.json
.venv/bin/python scripts/verify_catalog.py --manifest /path/to/run/manifest.json
```

`--check-only` on the importer performs preflight without database access.
Independent verification uses a read-only repeatable-read transaction and
compares every source record, original line hash, ordinal, typed projection,
import metadata, and active pointer. It rejects missing or extra products.

## Query examples

Use the active view for recommendation candidates. This example is a historical
USD budget filter, not a live-price or stock check:

```sql
SELECT product_id, title, historical_price, currency
FROM commerce.active_products
WHERE domain = 'beauty' AND family = 'skin_care'
  AND historical_price <= 30.00
ORDER BY historical_price, product_id
LIMIT 10;
```

Require explicit claims for a mandatory attribute. Missing/null/unscented claims
must not be interpreted as fragrance-free:

```sql
SELECT product_id, title, attributes -> 'fragrance_free' AS evidence
FROM commerce.active_products
WHERE attributes #>> '{fragrance_free,value}' = 'true';
```

Use driver parameters for values from user queries. Listing claims and derived
categories remain unverified; importing does not establish medical suitability,
current stock, complete variant availability, or current prices.

## Validation

```bash
.venv/bin/python -m pytest -q

# Opt in to real PostgreSQL tests; requires temporary database creation rights.
CATALOG_TEST_ADMIN_URL=postgresql:///postgres .venv/bin/python -m pytest -q
```

Integration tests create a randomly named `commerce_catalog_test_*` database
and remove it in fixture cleanup. They cover precise decimals, null claims,
repeated imports, active-snapshot replacement, rollback after insertion,
source-file mutation, database drift, migration checksums, and writer locks.
They do not reset the development catalog database.

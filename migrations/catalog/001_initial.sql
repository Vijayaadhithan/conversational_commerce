CREATE TABLE commerce.catalog_imports (
    import_id text PRIMARY KEY CHECK (import_id ~ '^[0-9a-f]{64}$'),
    run_id text NOT NULL,
    schema_version text NOT NULL CHECK (schema_version = '1.0.0'),
    subset_sha256 text NOT NULL CHECK (subset_sha256 ~ '^[0-9a-f]{64}$'),
    schema_sha256 text NOT NULL CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
    product_count integer NOT NULL CHECK (product_count > 0),
    manifest jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Keep the original normalized record, including claims, unknowns, and provenance.
-- Generated projections prevent typed filters from drifting from that record.
CREATE TABLE commerce.catalog_products (
    import_id text NOT NULL REFERENCES commerce.catalog_imports(import_id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    source_line_sha256 text NOT NULL CHECK (source_line_sha256 ~ '^[0-9a-f]{64}$'),
    product_id text GENERATED ALWAYS AS (document ->> 'product_id') STORED NOT NULL,
    parent_asin text GENERATED ALWAYS AS (document ->> 'parent_asin') STORED NOT NULL,
    title text GENERATED ALWAYS AS (document ->> 'title') STORED NOT NULL,
    domain text GENERATED ALWAYS AS (document #>> '{category,domain}') STORED NOT NULL,
    family text GENERATED ALWAYS AS (document #>> '{category,family}') STORED NOT NULL,
    brand text GENERATED ALWAYS AS (document #>> '{brand,value}') STORED,
    historical_price numeric GENERATED ALWAYS AS ((document #>> '{price,amount}')::numeric) STORED,
    currency text GENERATED ALWAYS AS (document #>> '{price,currency}') STORED,
    attributes jsonb GENERATED ALWAYS AS (document -> 'attributes') STORED NOT NULL,
    source_name text GENERATED ALWAYS AS (document #>> '{selected_observation,source}') STORED NOT NULL,
    PRIMARY KEY (import_id, product_id),
    UNIQUE (import_id, ordinal),
    CHECK (parent_asin ~ '^[A-Z0-9]{10}$'),
    CHECK (product_id = 'amazon:' || parent_asin),
    CHECK (length(btrim(title)) > 0),
    CHECK (domain IN ('beauty', 'fashion')),
    CHECK (family <> 'unknown' AND length(btrim(family)) > 0),
    CHECK (historical_price IS NOT NULL AND historical_price > 0),
    CHECK (currency IS NOT NULL AND currency = 'USD'),
    CHECK (document ->> 'schema_version' = '1.0.0'),
    CHECK (document #>> '{price,status}' = 'historical'),
    CHECK (document #>> '{price,parse_status}' = 'valid'),
    CHECK (document #> '{quality,eligible_for_subset}' = 'true'::jsonb),
    CHECK (document -> 'availability' = 'null'::jsonb)
);

CREATE INDEX catalog_products_category_price_idx
    ON commerce.catalog_products(import_id, domain, family, historical_price);
CREATE INDEX catalog_products_price_idx
    ON commerce.catalog_products(import_id, historical_price);
CREATE INDEX catalog_products_brand_idx
    ON commerce.catalog_products(import_id, lower(brand)) WHERE brand IS NOT NULL;

-- Switching this one row publishes a complete snapshot transactionally.
CREATE TABLE commerce.catalog_active (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    import_id text NOT NULL REFERENCES commerce.catalog_imports(import_id),
    activated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIEW commerce.active_products AS
    SELECT products.* FROM commerce.catalog_products AS products
    JOIN commerce.catalog_active AS active USING (import_id);

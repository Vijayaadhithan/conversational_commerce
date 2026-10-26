-- Product data stays in PostgreSQL; embeddings are immutable Parquet artifacts.
ALTER TABLE commerce.catalog_imports DROP CONSTRAINT catalog_imports_product_count_check;
ALTER TABLE commerce.catalog_imports ADD CONSTRAINT catalog_imports_product_count_check CHECK (product_count >= 0);

CREATE TABLE commerce.search_builds (
    build_id text PRIMARY KEY CHECK (build_id ~ '^[0-9a-f]{64}$'),
    import_id text NOT NULL REFERENCES commerce.catalog_imports(import_id),
    config jsonb NOT NULL,
    expected_count integer NOT NULL CHECK (expected_count >= 0),
    status text NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'ready')),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at timestamptz,
    UNIQUE (build_id, import_id)
);

CREATE TABLE commerce.search_documents (
    build_id text NOT NULL,
    import_id text NOT NULL,
    product_id text NOT NULL,
    source_line_sha256 text NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    title text NOT NULL,
    body text NOT NULL,
    input_clipped boolean NOT NULL,
    embedding_batch text NOT NULL CHECK (embedding_batch ~ '^[0-9a-f]{64}$'),
    embedding_reused boolean NOT NULL DEFAULT false,
    PRIMARY KEY (build_id, product_id),
    FOREIGN KEY (build_id, import_id) REFERENCES commerce.search_builds(build_id, import_id),
    FOREIGN KEY (import_id, product_id) REFERENCES commerce.catalog_products(import_id, product_id)
);

CREATE TABLE commerce.search_active (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    build_id text NOT NULL REFERENCES commerce.search_builds(build_id),
    activated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Qdrant is a derived index. Publication is recorded in the authoritative DB.
CREATE TABLE commerce.qdrant_indexes (
    collection_name text PRIMARY KEY,
    build_id text NOT NULL REFERENCES commerce.search_builds(build_id) ON DELETE CASCADE,
    contract_sha text NOT NULL CHECK (contract_sha ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'ready')),
    last_product_id text NOT NULL DEFAULT '',
    verified_count integer CHECK (verified_count >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verified_at timestamptz
);

CREATE INDEX search_documents_input_sha_idx ON commerce.search_documents(input_sha256,build_id);
CREATE INDEX search_documents_import_product_idx ON commerce.search_documents(import_id,product_id);
CREATE INDEX search_builds_import_idx ON commerce.search_builds(import_id);
CREATE INDEX qdrant_indexes_build_idx ON commerce.qdrant_indexes(build_id);

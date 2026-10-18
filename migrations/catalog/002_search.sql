CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE commerce.search_builds (
    build_id text PRIMARY KEY CHECK (build_id ~ '^[0-9a-f]{64}$'),
    import_id text NOT NULL REFERENCES commerce.catalog_imports(import_id),
    config jsonb NOT NULL,
    expected_count integer NOT NULL CHECK (expected_count > 0),
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
    embedding public.vector(768) NOT NULL CHECK (public.vector_norm(embedding) > 0),
    lexical tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, title), 'A') ||
        setweight(to_tsvector('english'::regconfig, body), 'B')
    ) STORED,
    PRIMARY KEY (build_id, product_id),
    FOREIGN KEY (build_id, import_id) REFERENCES commerce.search_builds(build_id, import_id),
    FOREIGN KEY (import_id, product_id) REFERENCES commerce.catalog_products(import_id, product_id)
);

CREATE INDEX search_documents_lexical_idx ON commerce.search_documents USING gin(lexical);

CREATE TABLE commerce.search_active (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    build_id text NOT NULL REFERENCES commerce.search_builds(build_id),
    activated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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

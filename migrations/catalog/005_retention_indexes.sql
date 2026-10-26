-- PostgreSQL does not create indexes on referencing foreign-key columns.
-- Without this index, deleting each retired product scans search_documents to
-- check whether any generation still references (import_id, product_id).
CREATE INDEX search_documents_import_product_idx
    ON commerce.search_documents(import_id, product_id);

CREATE INDEX search_builds_import_idx ON commerce.search_builds(import_id);
CREATE INDEX qdrant_indexes_build_idx ON commerce.qdrant_indexes(build_id);

-- A complete empty snapshot represents removal of every product.
ALTER TABLE commerce.catalog_imports DROP CONSTRAINT catalog_imports_product_count_check;
ALTER TABLE commerce.catalog_imports ADD CONSTRAINT catalog_imports_product_count_check CHECK (product_count >= 0);
ALTER TABLE commerce.search_builds DROP CONSTRAINT search_builds_expected_count_check;
ALTER TABLE commerce.search_builds ADD CONSTRAINT search_builds_expected_count_check CHECK (expected_count >= 0);

ALTER TABLE commerce.search_documents ADD COLUMN embedding_reused boolean NOT NULL DEFAULT false;
CREATE INDEX search_documents_input_sha_idx ON commerce.search_documents(input_sha256,build_id);

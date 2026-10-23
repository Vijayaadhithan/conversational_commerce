"""Compatibility with the embedding contract used by existing search generations."""

from test_catalog import product

from app.embedding_text import contract, document_text


def test_existing_generation_contract_is_preserved():
    assert contract() == {
        "version": "commerce-listing-v1",
        "sha256": "c52e9a83972bfa8e12e51b5c19783d53a97eb77d594d0b6035c61175888b8ab9",
        "dimensions": 768,
        "document_bytes": 1536,
        "lexical_body_bytes": 8192,
        "query_prefix": "task: search result | query: ",
    }
    selected = document_text(product())
    assert selected["input"] == (
        "title: Example moisturizer | text: beauty\nskin care\nAn explicit listing description."
    )
    assert selected["body"] == "beauty\nskin care\nAn explicit listing description."
    assert selected["clipped"] is False

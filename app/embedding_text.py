"""Versioned selection of listing text for retrieval; source records stay intact."""

import hashlib

DIMENSIONS = 768
DOCUMENT_BYTES = 1536
LEXICAL_BODY_BYTES = 8192
QUERY_PREFIX = "task: search result | query: "
INPUT_VERSION = "commerce-listing-v1"
# Stable v1 identifier, retained from the original implementation.
# Update the version and identifier when embedding input behavior changes.
INPUT_CONTRACT_SHA256 = "c52e9a83972bfa8e12e51b5c19783d53a97eb77d594d0b6035c61175888b8ab9"


def clip_utf8(text, budget):
    return text.encode("utf-8")[: max(0, budget)].decode("utf-8", errors="ignore")


def contract():
    return {
        "version": INPUT_VERSION,
        "sha256": INPUT_CONTRACT_SHA256,
        "dimensions": DIMENSIONS,
        "document_bytes": DOCUMENT_BYTES,
        "lexical_body_bytes": LEXICAL_BODY_BYTES,
        "query_prefix": QUERY_PREFIX,
    }


def document_text(record):
    title = clip_utf8(record["title"], 512)
    attribute_clipped = False
    parts = [record["category"]["domain"], record["category"]["family"].replace("_", " ")]
    if record["brand"]:
        parts.append("Brand: " + record["brand"]["value"])
    for name, claim in sorted(record["attributes"].items()):
        if claim is not None:
            value = (
                str(claim["value"]).lower() if isinstance(claim["value"], bool) else claim["value"]
            )
            attribute_clipped = attribute_clipped or clip_utf8(value, 192) != value
            parts.append(name.replace("_", " ") + ": " + clip_utf8(value, 192))
    parts.extend(record["content"]["features"])
    parts.extend(record["content"]["description"])
    full_body = "\n".join(parts)
    prefix = f"title: {title} | text: "
    body = clip_utf8(full_body, DOCUMENT_BYTES - len(prefix.encode("utf-8")))
    embedding_input = prefix + body
    lexical_body = clip_utf8(full_body, LEXICAL_BODY_BYTES)
    clipped = attribute_clipped or title != record["title"] or body != full_body
    return {
        "title": title,
        "body": lexical_body,
        "input": embedding_input,
        "sha256": hashlib.sha256(embedding_input.encode("utf-8")).hexdigest(),
        "clipped": clipped,
    }

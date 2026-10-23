"""Rerank only eligible retrieved products, preserving their IDs and provenance."""

import math
import time

from app.catalog import CatalogError
from app.embedding_text import clip_utf8

DOCUMENT_VERSION = "commerce-rerank-v2"


def rerank_document(record):
    # Put identity/product type first. All excerpts remain unverified listing claims.
    parts = [
        "Title: " + clip_utf8(record["title"], 768),
        "Category: " + record["category"]["domain"] + "/" + record["category"]["family"],
    ]
    if record["brand"]:
        parts.append("Brand: " + clip_utf8(record["brand"]["value"], 192))
    # Keep concise attributes ahead of long marketing descriptions.
    for name, claim in sorted(record["attributes"].items()):
        if claim is not None:
            parts.append(name.replace("_", " ") + ": " + clip_utf8(str(claim["value"]), 192))
    parts.extend(record["content"]["features"])
    parts.extend(record["content"]["description"])
    return clip_utf8("\n".join(parts), 4000)


def rerank_candidates(response, documents, request, engine, *, defer_limit=False):
    candidates = response["results"]
    if not candidates:
        response["reranking"] = {
            "requested": True,
            "applied": False,
            "reason": "no_candidates",
            "candidate_count": 0,
        }
        return response
    started = time.perf_counter()
    try:
        texts = [documents[row["product_id"]] for row in candidates]
        ranked = engine.rank(request.query, texts, len(texts), preserve_query=True)
        rows = ranked["results"]
        if len(rows) != len(candidates):
            raise ValueError("Incomplete reranker output")
        scores = {}
        for row in rows:
            index, score = row["index"], row["score"]
            if (
                type(index) is not int
                or not 0 <= index < len(candidates)
                or index in scores
                or type(score) not in (int, float)
                or not math.isfinite(score)
                or row.get("text", texts[index]) != texts[index]
            ):
                raise ValueError("Invalid reranker output")
            scores[index] = float(score)
        order = sorted(scores, key=lambda index: (-scores[index], index))
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise CatalogError(
            "Product reranking unavailable or invalid. Check the selected provider, or explicitly set rerank=false."
        ) from exc
    for rank, index in enumerate(order, 1):
        candidates[index]["ranking"].update(
            rrf_rank=index + 1, rerank_rank=rank, rerank_score=scores[index]
        )
    response["results"] = [
        candidates[index] for index in (order if defer_limit else order[: request.limit])
    ]
    response["method"] += "+cross_encoder"
    response["reranking"] = {
        "requested": True,
        "applied": True,
        "candidate_count": len(candidates),
        "model": ranked.get("model"),
        "revision": ranked.get("revision"),
        "device": ranked.get("device"),
        "device_note": ranked.get("device_note"),
        "max_pair_tokens": ranked.get("max_pair_tokens"),
        "document_version": DOCUMENT_VERSION,
        "seconds": round(time.perf_counter() - started, 4),
    }
    return response

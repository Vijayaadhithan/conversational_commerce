"""One bounded retrieval recovery without relaxing any validated requirement."""

from app.search_normalization import normalize_search


def quality_summary(response):
    rows = response.get("results", [])
    return {
        "returned": len(rows),
        "supported": sum(
            row.get("listing_quality", {}).get("supported_concerns", 0) for row in rows
        ),
        "contradicted": sum(
            row.get("listing_quality", {}).get("contradicted_concerns", 0) for row in rows
        ),
        "cautioned": sum(bool(row.get("listing_quality", {}).get("cautions")) for row in rows),
    }


def search_with_recovery(search, request, literal_query):
    first = search(request)
    summary = quality_summary(first)
    fallback = normalize_search(literal_query, storefront_id=request.filters.storefront_id)
    known_quality = all("listing_quality" in row for row in first["results"])
    weak = not first["results"] or (
        known_quality and bool(request.concerns) and not summary["supported"]
    )
    if (
        not weak
        or not fallback
        or fallback == normalize_search(request.query, storefront_id=request.filters.storefront_id)
        or len(fallback.encode()) > 1000
    ):
        return first
    second = search(request.model_copy(update={"query": fallback}))
    candidates = [first, second]

    def score(response):
        item = quality_summary(response)
        return (
            bool(item["returned"]),
            -item["contradicted"],
            item["supported"],
            -item["cautioned"],
        )

    selected = max(range(2), key=lambda index: score(candidates[index]))
    result = dict(candidates[selected])
    result["retrieval_recovery"] = {
        "selected_attempt": selected + 1,
        "attempts": [
            {"query": query, **quality_summary(response)}
            for query, response in zip((request.query, fallback), candidates, strict=True)
        ],
        "constraints_relaxed": False,
    }
    return result

"""Mechanical routing/isolation checks, deliberately separate from human relevance review."""

from app.storefronts import category_allowed


def check_storefront_response(scope, payload, expected_destinations=None):
    commerce = payload.get("commerce", {})
    if not isinstance(commerce, dict) or not isinstance(commerce.get("action"), str):
        return {
            "has_commerce_response": False,
            "no_cross_storefront_products": None,
            "expected_destinations_match": None,
            "observed_destinations": [],
            "action": None,
            "human_relevance_review_required": True,
        }
    products = commerce.get("products", [])
    for group in commerce.get("groups", []):
        products = [*products, *group.get("products", [])]
    observed = sorted(
        {
            target.get("storefront_id", "")
            for target in commerce.get("handoff", {}).get("targets", [])
        }
    )
    return {
        "has_commerce_response": bool(commerce),
        "no_cross_storefront_products": all(
            category_allowed(row.get("category"), scope) for row in products
        ),
        "expected_destinations_match": (observed == sorted(expected_destinations))
        if expected_destinations is not None
        else None,
        "observed_destinations": observed,
        "action": commerce.get("action"),
        "human_relevance_review_required": True,
    }

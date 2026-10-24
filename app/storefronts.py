"""Application-owned storefront taxonomy, independent of catalog domains."""

from typing import Literal

StorefrontID = Literal["clothing", "beauty", "footwear", "fashion"]

STOREFRONTS = {
    "clothing": {"label": "Clothing", "domain": "fashion", "families": ("apparel",)},
    "beauty": {
        "label": "Beauty & personal care",
        "domain": "beauty",
        "families": (
            "skin_care",
            "hair_care",
            "makeup",
            "fragrance",
            "oral_care",
            "shaving",
            "beauty_tools",
            "personal_care",
        ),
    },
    "footwear": {"label": "Footwear", "domain": "fashion", "families": ("footwear",)},
    "fashion": {
        "label": "Fashion accessories",
        "domain": "fashion",
        "families": ("bags", "jewelry", "watches", "accessories"),
    },
}
FAMILY_STOREFRONT = {
    family: key for key, section in STOREFRONTS.items() for family in section["families"]
}


def category_allowed(category, storefront_id):
    if storefront_id is None:
        return True
    section = STOREFRONTS[storefront_id]
    return (
        isinstance(category, dict)
        and category.get("family") in section["families"]
        and category.get("domain") == section["domain"]
    )


def filters_allowed(filters, storefront_id):
    section = STOREFRONTS[storefront_id]
    return (filters.family is None or filters.family in section["families"]) and (
        filters.domain is None or filters.domain == section["domain"]
    )

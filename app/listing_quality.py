"""Listing identity and visible ranking cautions, never inferred suitability."""
import re


# Alternatives within one entry describe the same product, not a multi-product kit.
PRODUCT_TYPES = {
    "skin_care": [r"moisturi[sz]er|face cream|face lotion", r"cleanser", r"serum", r"sunscreen",
                  r"body lotion", r"face mask", r"(?:chemical |acid |facial )?peel"],
    "hair_care": [r"shampoo", r"conditioner", r"hair oil", r"hair spray", r"hair dye", r"wig"],
    "makeup": [r"lipstick", r"lip gloss", r"mascara", r"eyeliner", r"foundation", r"nail polish"],
    "apparel": [r"t[ -]?shirt|shirt|tee", r"blouse", r"dress", r"jeans", r"pants", r"jacket",
                r"hoodie", r"sweater", r"skirt", r"leggings", r"shorts"],
    "footwear": [r"running[ -](?:shoe|sneaker)|sneaker|shoe", r"sandal", r"boot", r"slipper", r"loafer"],
    "bags": [r"handbag|purse|cross[ -]?body(?: bag)?|shoulder bag", r"backpack", r"wallet", r"tote bag"],
    "jewelry": [r"necklace|necklace pendant", r"earring", r"bracelet", r"ring", r"brooch"],
    "fragrance": [r"perfume|eau de parfum|eau de toilette", r"cologne"],
    "shaving": [r"razor blade|replacement blade|razor cartridge", r"razor|shaver"],
}
ACCESSORIES = re.compile(
    r"\b(?:shoe charms?|lipstick (?:holders?|containers?|brooch)|(?:handbag|purse|bag) (?:organizers?|inserts?|liners?)|"
    r"empty (?:lipstick )?tubes?|shoe (?:cleaners?|laces?|insoles?))\b", re.I)
CHILD = r"\b(?:girls?|boys?|kids?|child(?:ren)?|toddlers?|junior|baby|infant)\b"
BUNDLE = r"\b(?:sets?|kits?|\d+[ -]?(?:piece|pcs?|pack))\b"
PERSONALIZED = r"\b(?:personalized|personalised|custom(?:ized|ised)?|nameplate)\b"


def title_product_type(title, family):
    accessory = ACCESSORIES.search(title)
    if accessory:
        return accessory.group()
    matches = [match for pattern in PRODUCT_TYPES.get(family, [])
               if (match := re.search(r"\b(?:" + pattern + r")s?\b", title, re.I))]
    return matches[0].group() if len(matches) == 1 else None


def keyword_heavy(text):
    words = re.findall(r"[a-z]+", text.lower())
    # Repeated keyword inventories are weaker evidence than a descriptive clause.
    repeated = any(words.count(word) >= 5 for word in set(words)
                   if word not in {"the", "a", "and", "of", "to", "for", "with", "in", "is", "your"})
    inventory = len(set(words) & {"sale", "prime", "clearance", "price", "cheap", "buy"}) >= 3 and not re.search(r"[.!?;]", text)
    return len(words) >= 30 and (repeated or inventory)


def listing_quality(record, request, evidence):
    title = record.get("title", "")
    goal = request.product_goal
    context = " ".join([goal, *request.concerns, *map(str, request.filters.attributes.values())])
    cautions = []

    def caution(code, text, field="/title", excerpt=title):
        cautions.append({"code": code, "text": text, "evidence": {"field": field, "text": excerpt}})

    if goal:
        if re.search(BUNDLE, title, re.I) and not re.search(BUNDLE + r"|\bbundle\b", context, re.I):
            caution("bundle", "This listing is a set or multipack; check its included items and quantities.")
        if re.search(PERSONALIZED, title, re.I) and not re.search(PERSONALIZED, context, re.I):
            caution("personalization", "This listing requires personalization, which you have not requested.")
        if re.search(CHILD, title, re.I) and not re.search(CHILD, context, re.I):
            # Mixed women/girls marketing does not establish a child-only item.
            if not re.search(r"\b(?:women|men|adult|unisex)\b", title, re.I):
                caution("child_sizing", "This listing names a child recipient; check the intended recipient and sizing.")
        if re.search(r"\b(?:chemical|acid) peel\b", title, re.I) and not re.search(r"\bpeel\b", context, re.I):
            caution("unrequested_format", "This is a chemical/acid peel, a specific product format you have not requested; ingredient preferences remain unverified.")
    if "conflicting_material_composition" in record.get("quality", {}).get("warnings", []):
        sources = [(field, text) for name in ("features", "description")
                   for field, text in zip(record.get("content_source_fields", {}).get(name, []), record.get("content", {}).get(name, []))
                   if re.search(r"\d+%\s*(?:cotton|polyester|spandex|elastane|nylon|linen|wool|silk|rayon|viscose|acrylic)", text, re.I)]
        field, text = sources[0] if sources else ("/title", title)
        caution("conflicting_material", "The listing contains conflicting material composition claims; composition is unconfirmed.", field, text)
        cautions[-1]["other_evidence"] = [{"field": f, "text": t} for f, t in sources[1:2]]
    supported = sum(item.get("support") == "positive" for item in evidence)
    contradicted = sum(item.get("support") in {"negated", "mixed"} for item in evidence)
    return {"cautions": cautions, "supported_concerns": supported, "contradicted_concerns": contradicted,
            "unverified_concerns": sum(item.get("support") != "positive" for item in evidence)}


def prioritize_listing_evidence(response, request):
    """Stable order within quality groups preserves the model/RRF ranking.

    Soft concerns are never eligibility filters. Partial matches remain available
    with explicit explanations; hard filters have already bounded this candidate set.
    """
    if not request.product_goal and not request.concerns:
        response["results"] = response["results"][:request.limit]
        return response
    def key(row):
        quality = row.get("listing_quality", {})
        cautions = quality.get("cautions", [])
        serious = any(c["code"] in {"conflicting_material", "child_sizing", "unrequested_format"} for c in cautions)
        return (bool(quality.get("contradicted_concerns")) or serious,
                -quality.get("supported_concerns", 0), bool(cautions))
    response["results"] = sorted(response["results"], key=key)[:request.limit]
    for rank, row in enumerate(response["results"], 1):
        row["ranking"]["evidence_rank"] = rank
    response["method"] += "+listing_evidence"
    return response

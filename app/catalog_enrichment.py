"""Conservative, reproducible enrichment of selected historical observations.

No ingredient-to-outcome inference, cross-observation merging, or variant expansion.
The v1 claim contract already supports additional named, source-backed attributes.
"""

import re
from copy import deepcopy
from html import unescape

from app.listing_quality import title_product_type

VERSION = "listing-enrichment-v2"
DETAIL_ALIASES = {
    "ingredients": ("ingredients", "ingredient list"),
    "inactive_ingredients": ("inactive ingredients",),
    "active_ingredients": ("active ingredients", "active ingredient"),
    "featured_ingredients": ("special ingredients",),
    "skin_type": ("skin type", "skin types"),
    "hair_type": ("hair type", "hair types"),
    "benefits": ("product benefits",),
    "size": ("size", "apparel size", "shoe size"),
    "color": ("color", "colour"),
    "volume": ("item volume", "liquid volume", "volume"),
    "style": ("style",),
    "material": ("material", "fabric type", "fabric composition", "material composition"),
    "department": ("department", "target gender"),
    "age_range": ("age range", "age range (description)"),
    "size_options": ("available sizes", "sizes available"),
    "color_options": ("available colors", "available colours", "colors available"),
}
# Explicit labeled spans only. Never treat a marketing ingredient mention as a list.
LABELS = {
    "ingredients": r"(?:product )?ingredients|ingredient list",
    "inactive_ingredients": r"inactive ingredients",
    "active_ingredients": r"active ingredients?",
    "skin_type": r"skin types?",
    "hair_type": r"hair types?",
    "material": r"material|fabric type|fabric composition|material composition",
    "size": r"size",
    "volume": r"(?:item |liquid )?volume",
    "size_options": r"available sizes|sizes available",
    "color_options": r"available colou?rs|colou?rs available",
}
NEGATION = re.compile(r"\b(?:not|no|never|avoid|except|unsuitable)\b|n't\b", re.I)
PROSE = re.compile(
    r"\b(?:we|our|you|your|please|note|stock|return|shipping|using|focus|apply|directions|include[sd]?)\b",
    re.I,
)


def ingredient_value(value):
    """Reject marketing/section headings; retain a list or a named single ingredient."""
    if re.search(r"\b(?:formulated|contains?|provide|format|purpose|long-lasting)\b", value, re.I):
        return False
    if "," in value:
        return True
    return bool(
        re.fullmatch(
            r"(?:(?:100%|pure|organic|unrefined)\s+)*(?:petrolatum|water|alcohol|glycerin|lanolin|caffeine|"
            r"(?:[a-z]+\s+){1,5}(?:oil|butter|acid|extract))",
            value,
            re.I,
        )
    )


def labeled_values(text, label):
    # Decimal points and abbreviations inside ingredient lists are not sentence ends.
    boundary = r"[;!?]|\.(?=\s+[A-Z]|\s*$)"
    for span in re.split(boundary, text):
        match = re.fullmatch(r"\s*(?:" + label + r")\s*:\s*(.+?)\s*", span, re.I)
        if match:
            value = match.group(1)
            if 0 < len(value) <= 2000 and ":" not in value and not PROSE.search(value):
                yield value


def clean(value):
    if not isinstance(value, str):
        return None
    value = " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())
    return value if value and value.lower() not in {"n/a", "unknown", "none", "null"} else None


def claim(value, field):
    return {"value": value, "field": field, "status": "listing_claim"}


def unique_claim(candidates):
    """A disagreement stays unknown; no arbitrary first-value choice."""
    values = {item["value"].casefold() for item in candidates}
    return candidates[0] if len(values) == 1 else None


def enrich_record(record, raw):
    product = deepcopy(record)
    attributes = product["attributes"]
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    for name, aliases in DETAIL_ALIASES.items():
        if attributes.get(name):
            continue
        candidates = [
            claim(value, "/details/" + key.replace("~", "~0").replace("/", "~1"))
            for key, raw_value in details.items()
            if key.strip().casefold() in aliases and (value := clean(raw_value))
        ]
        if candidates:
            attributes[name] = unique_claim(candidates)
    # Fix the old ETL/API naming mismatch without removing the original claim.
    if not attributes.get("benefits") and attributes.get("product_benefits"):
        attributes["benefits"] = deepcopy(attributes["product_benefits"])
    fields = []
    for name in ("features", "description"):
        fields.extend(
            zip(record["content_source_fields"][name], record["content"][name], strict=True)
        )
    for name, label in LABELS.items():
        if attributes.get(name):
            continue
        candidates = []
        for index, (field, text) in enumerate(fields):
            # Some source descriptions separate a heading and its ingredient list.
            if (
                name in {"ingredients", "active_ingredients", "inactive_ingredients"}
                and re.fullmatch(r"\s*(?:" + label + r")\s*:?\s*", text, re.I)
                and index + 1 < len(fields)
            ):
                following_field, following = fields[index + 1]
                if following_field.rsplit("/", 1)[0] == field.rsplit("/", 1)[0]:
                    for value in labeled_values(name.replace("_", " ") + ": " + following, label):
                        if ingredient_value(value):
                            candidates.append(claim(value, following_field))
            for value in labeled_values(text, label):
                if name in {"ingredients", "inactive_ingredients"} and not ingredient_value(value):
                    continue
                if name == "size":
                    # Options and bottle contents are separate from a selected size.
                    if re.search(
                        r"[,/]|\b(?:ml|liters?|litres?|fl\.?\s*oz|fluid ounces?)\b", value, re.I
                    ):
                        continue
                    if len(value) > 80:
                        continue
                candidates.append(claim(value, field))
        if candidates:
            attributes[name] = unique_claim(candidates)
    # Keep coordinated types as one literal claim rather than dropping all but one.
    for name, noun, types in (
        ("skin_type", "skin", "dry|oily|sensitive|combination|normal|mature"),
        (
            "hair_type",
            "hair",
            "dry|oily|curly|straight|wavy|coily|damaged|fine|thick|color-treated",
        ),
    ):
        if attributes.get(name):
            continue
        candidates = []
        pattern = (
            r"\b(?:for |suits |suitable for )?((?:"
            + types
            + r")(?:\s*(?:,\s*(?:and |or )?|/|&|and|or)\s*(?:"
            + types
            + r"))*) "
            + noun
            + r"\b"
        )
        for field, text in [("/title", record["title"]), *fields]:
            for sentence in re.split(r"[.!?;]", text):
                if NEGATION.search(sentence):
                    continue
                if len(re.findall(r"\b" + noun + r"\b", sentence, re.I)) > 1:
                    continue
                for match in re.finditer(pattern, sentence, re.I):
                    if field != "/title" and not re.match(
                        r"(?:for |suits |suitable for )", match.group(), re.I
                    ):
                        continue
                    if re.match(r"\s*(?:,|/|\band\b|\bor\b)", sentence[match.end() :]):
                        continue
                    candidates.append(claim(match.group(1), field))
        if candidates:
            attributes[name] = unique_claim(candidates)
    if not attributes.get("product_type"):
        value = title_product_type(record["title"], record["category"]["family"])
        if value:
            attributes["product_type"] = claim(value, "/title")
    if record["category"]["domain"] == "fashion" and not attributes.get("department"):
        # Only title identity, never gift-recipient or accessory-compatibility prose.
        title = re.split(r"\b(?:gift|for)\b", record["title"], maxsplit=1, flags=re.I)[0]
        recipients = [
            m.group() for m in re.finditer(r"\b(?:women|men|girl|boy)(?:['’]?s)?\b", title, re.I)
        ]
        if recipients:
            attributes["department"] = unique_claim([claim(v, "/title") for v in recipients])
    # A concise standalone composition is useful; disagreement is explicitly flagged.
    compositions = []
    material_pattern = r"\d+(?:\.\d+)?%\s*(?:cotton|polyester|spandex|elastane|nylon|linen|wool|silk|rayon|viscose|acrylic)"
    for field, text in fields:
        parts = list(re.finditer(material_pattern, text, re.I))
        if parts:
            percentages = [
                (float(m.group().split("%")[0]), m.group().split("%")[1].strip().lower())
                for m in parts
            ]
            # Do not combine lining/shell or a list of unrelated compositions.
            if abs(sum(v for v, _ in percentages) - 100) < 0.01:
                compositions.append(
                    (
                        tuple(sorted(percentages)),
                        claim(text[parts[0].start() : parts[-1].end()], field),
                    )
                )
    if len({v for v, _ in compositions}) > 1:
        # Upstream conflicting_fields rejects an entire import. Keep this derived
        # textual disagreement as a warning, without inventing a material value.
        product["quality"]["warnings"].append("conflicting_material_composition")
    elif compositions and not attributes.get("material"):
        attributes["material"] = compositions[0][1]
    return product

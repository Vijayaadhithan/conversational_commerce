"""Conservative, reproducible enrichment of selected historical observations.

No ingredient-to-outcome inference, cross-observation merging, or variant expansion.
The v1 claim contract already supports additional named, source-backed attributes.
"""

from copy import deepcopy
from html import unescape
import re

VERSION = "listing-enrichment-v1"
DETAIL_ALIASES = {
    "ingredients": ("ingredients", "ingredient list"),
    "active_ingredients": ("active ingredients", "active ingredient"),
    "featured_ingredients": ("special ingredients",),
    "skin_type": ("skin type", "skin types"),
    "hair_type": ("hair type", "hair types"),
    "benefits": ("product benefits",),
    "size": ("size", "apparel size", "shoe size"),
    "color": ("color", "colour"),
    "volume": ("item volume", "liquid volume", "volume"),
    "style": ("style",),
    "size_options": ("available sizes", "sizes available"),
    "color_options": ("available colors", "available colours", "colors available"),
}
# Explicit labeled spans only. Never treat a marketing ingredient mention as a list.
LABELS = {
    "ingredients": r"ingredients|ingredient list",
    "active_ingredients": r"active ingredients?",
    "skin_type": r"skin types?",
    "hair_type": r"hair types?",
    "size": r"size",
    "volume": r"(?:item |liquid )?volume",
    "size_options": r"available sizes|sizes available",
    "color_options": r"available colou?rs|colou?rs available",
}
TYPES = {
    "skin_care": r"moisturizer|moisturiser|cleanser|serum|sunscreen|face cream|body lotion|face mask",
    "hair_care": r"shampoo|conditioner|hair oil|hair spray|hair dye|wig",
    "makeup": r"lipstick|lip gloss|mascara|eyeliner|foundation|nail polish",
    "apparel": r"t-shirt|shirt|blouse|dress|jeans|pants|jacket|hoodie|sweater|skirt|leggings",
    "footwear": r"sneakers?|sandals?|boots?|slippers?|loafers?",
    "bags": r"backpack|handbag|wallet|tote bag|shoulder bag",
    "fragrance": r"perfume|cologne|eau de parfum|eau de toilette",
}
NEGATION = re.compile(r"\b(?:not|no|never|avoid|except|unsuitable)\b|n't\b", re.I)
PROSE = re.compile(r"\b(?:we|our|you|your|please|note|stock|return|shipping|using|focus|apply|directions|include[sd]?)\b", re.I)


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
    if not isinstance(value, str): return None
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
        if attributes.get(name): continue
        candidates = [claim(value, "/details/" + key.replace("~", "~0").replace("/", "~1"))
                      for key, raw_value in details.items()
                      if key.strip().casefold() in aliases and (value := clean(raw_value))]
        if candidates:
            attributes[name] = unique_claim(candidates)
    # Fix the old ETL/API naming mismatch without removing the original claim.
    if not attributes.get("benefits") and attributes.get("product_benefits"):
        attributes["benefits"] = deepcopy(attributes["product_benefits"])
    fields = []
    for name in ("features", "description"):
        fields.extend(zip(record["content_source_fields"][name], record["content"][name], strict=True))
    for name, label in LABELS.items():
        if attributes.get(name): continue
        candidates = []
        for field, text in fields:
            for value in labeled_values(text, label):
                if name == "ingredients" and "," not in value: continue
                if name == "size":
                    # Options and bottle contents are separate from a selected size.
                    if re.search(r"[,/]|\b(?:ml|liters?|litres?|fl\.?\s*oz|fluid ounces?)\b", value, re.I): continue
                    if len(value) > 80: continue
                candidates.append(claim(value, field))
        if candidates: attributes[name] = unique_claim(candidates)
    for name, noun, types in (("skin_type", "skin", "dry|oily|sensitive|combination|normal"),
                              ("hair_type", "hair", "dry|oily|curly|straight|wavy|damaged|fine|thick")):
        if attributes.get(name): continue
        candidates = []
        for field, text in fields:
            for sentence in re.split(r"[.!?;]", text):
                if NEGATION.search(sentence): continue
                # Single explicit type only; multi-type claims must stay intact.
                for match in re.finditer(r"\bfor (?:" + types + r") " + noun + r"\b", sentence, re.I):
                    if re.match(r"\s*(?:,|/|\band\b|\bor\b)", sentence[match.end():]): continue
                    candidates.append(claim(match.group()[4:-(len(noun) + 1)], field))
        if candidates: attributes[name] = unique_claim(candidates)
    patterns = TYPES.get(record["category"]["family"])
    if patterns and not attributes.get("product_type"):
        matches = [claim(match.group(), "/title") for match in re.finditer(r"\b(?:" + patterns + r")\b", record["title"], re.I)]
        if matches: attributes["product_type"] = unique_claim(matches)
    return product

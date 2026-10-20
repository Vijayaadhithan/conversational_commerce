"""Concern-specific excerpts, never a prediction of suitability or effectiveness."""

import re

from app.listing_quality import keyword_heavy


# These are text-topic aliases, not ingredient -> benefit or medical inference rules.
TOPICS = (
    (r"\b(?:lighten\w*|brighten\w*)\b", r"\b(?:lighten\w*|brighten\w*|brightness|radiance)\b|\beven(?:s|ing)? (?:out )?(?:skin )?tone\b", "brightening / lightening wording"),
    (r"\b(?:pimples?|pimble|acne|blemishes?|breakouts?)\b", r"\b(?:pimples?|acne|blemishes?|breakouts?)\b", "acne / blemish wording"),
    (r"\bdry skin\b", r"\bdry(?:\s*(?:,|&|and|or)\s*(?:sensitive|normal|combination|oily))*\s+skin\b|\bskin dryness\b", "dry skin"),
    (r"\bcurly hair\b", r"\bcurly(?:\s*(?:,|&|and|or)\s*(?:wavy|coily|kinky))*\s+hair\b", "curly hair"),
    (r"\bsensitive skin\b", r"\bsensitive skin\b", "sensitive skin"),
    (r"\boily skin\b", r"\boily skin\b", "oily skin"),
    (r"\b(?:frizz|frizzy)\b", r"\b(?:frizz|frizzy)\b", "frizz wording"),
    (r"\bfragrance[ -]free\b", r"\bfragrance[ -]free\b|\bfree (?:of|from) (?:added )?fragrance\b", "fragrance-free wording"),
    (r"\bpockets?\b", r"\b(?:pockets?|pocketed)\b", "pocket wording"),
)
VAGUE = re.compile(r"\b(?:(?:less|kess|fewer|low|no)\s+chemi\w*|gentler|not too strong)\b", re.I)


def support_for(text, match):
    # Local clause only: a negation in a different sentence cannot negate this claim.
    start = max(text.rfind(mark, 0, match.start()) for mark in (".", ";", "!", "?", "\n")) + 1
    prefix = text[start:match.start()]
    qualifiers = r"(?:a|an|the|recommended|suitable|suited|intended|designed|safe|ideal|meant|for|use|using|on|with|very|to|treat|dry|oily|sensitive|normal|and|or)"
    if re.search(r"\b(?:not|never|avoid|except|unsuitable)\s+(?:" + qualifiers + r"[ ,]+){0,7}$|"
                 r"\b(?:no|without)\s+(?:(?:added|visible|any)\s+)?$|n't\s+(?:" + qualifiers + r"\s+){0,5}$", prefix, re.I):
        return "negated"
    return "weak" if keyword_heavy(text) else "positive"


def evidence_fields(record):
    """Yield full source fields internally; only bounded matching excerpts leave search."""
    yield "/title", record.get("title", "")
    for name, claim in record.get("attributes", {}).items():
        if claim and isinstance(claim["value"], str):
            yield claim["field"], claim["value"]
    for name in ("features", "description"):
        yield from zip(record.get("content_source_fields", {}).get(name, []),
                       record.get("content", {}).get(name, []), strict=True)
    # Legacy persisted cards / lightweight search adapters only have excerpts.
    for item in record.get("listing_evidence", []):
        yield item["field"], item["text"]


def concern_evidence(record, concerns):
    fields = list(dict.fromkeys(evidence_fields(record)))
    result = []
    for concern in dict.fromkeys(concerns):
        item = {"concern": concern, "status": "not_stated", "support": "unknown", "evidence": [], "topics": []}
        if VAGUE.search(concern):
            item["status"] = "needs_clarification"
            result.append(item)
            continue
        for name, noun in (("skin_type", "skin"), ("hair_type", "hair")):
            claim = record.get("attributes", {}).get(name)
            if not claim or not isinstance(claim["value"], str): continue
            value = claim["value"]
            types = re.split(r"\s*(?:,|/|&|\band\b|\bor\b)\s*", value)
            if len(value) <= 100 and any(re.fullmatch(r"[\w -]+", v) and re.search(
                    r"\b" + re.escape(v) + r"\s+" + noun + r"\b", concern, re.I) for v in types if v):
                item["evidence"].append({"field": claim["field"], "text": value, "status": "listing_excerpt", "attribute": name, "support": "positive"})
                item["topics"].append(name.replace("_", " "))
        topics = [(pattern, label) for trigger, pattern, label in TOPICS if re.search(trigger, concern, re.I)]
        # Exact phrases remain useful for fashion/materials and unlisted concerns.
        phrase = " ".join(concern.split())
        if len(phrase) >= 3:
            topics.append((r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)", "exact wording"))
        for pattern, label in topics:
            for field, text in fields:
                for match in re.finditer(pattern, text, re.I):
                    if match.end() - match.start() > 400:
                        continue
                    # Keep the clause, including negations. A mention is never a positive claim.
                    start = max(0, match.start() - 100)
                    while start > 0 and not text[start - 1].isspace() and match.start() - start < 130:
                        start -= 1
                    end = min(len(text), max(start + 320, match.end() + 100))
                    if end - start > 600: end = start + 600
                    excerpt = {"field": field, "text": text[start:end], "status": "listing_excerpt", "support": support_for(text, match)}
                    if label not in item["topics"]: item["topics"].append(label)
                    if excerpt not in item["evidence"]:
                        item["evidence"].append(excerpt)
        if item["evidence"]:
            item["status"] = "listing_mention"
            supports = {e["support"] for e in item["evidence"]}
            item["support"] = ("mixed" if {"positive", "negated"} <= supports else
                               "negated" if "negated" in supports else "positive" if "positive" in supports else "weak")
            # Preserve contradictory excerpts even when positive mentions came first.
            ordered = sorted(item["evidence"], key=lambda e: (e["support"] != "negated", e["support"] != "positive"))
            item["evidence"] = ordered[:2]
        result.append(item)
    return result


def concern_reasons(items):
    reasons = []
    for item in items:
        prefix = f"For your concern ‘{item['concern']}’: "
        if item["status"] == "needs_clarification":
            reasons.append(prefix + "please specify the ingredient or property you want to avoid; this preference is not a verified listing attribute.")
        elif item["evidence"]:
            quotes = " ".join(f"“{e['text']}” ({e['field']})." for e in item["evidence"])
            label = {"negated": "the listing warns against or negates this requirement — ",
                     "mixed": "the listing contains conflicting wording for this requirement — ",
                     "weak": "only keyword-heavy wording was found; this is weak evidence — "}.get(item.get("support"), "related listing wording — ")
            reasons.append(prefix + label + quotes + " These mentions do not explicitly confirm suitability or results for you.")
        else:
            reasons.append(prefix + "the available listing fields do not explicitly confirm suitability; no matching evidence was found.")
    return reasons

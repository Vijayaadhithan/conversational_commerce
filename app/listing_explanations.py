"""Concern-specific excerpts, never a prediction of suitability or effectiveness."""

import re


# These are text-topic aliases, not ingredient -> benefit or medical inference rules.
TOPICS = (
    (r"\b(?:lighten\w*|brighten\w*)\b", r"\b(?:lighten\w*|brighten\w*)\b", "brightening / lightening wording"),
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
        item = {"concern": concern, "status": "not_stated", "evidence": [], "topics": []}
        if VAGUE.search(concern):
            item["status"] = "needs_clarification"
            result.append(item)
            continue
        for name, noun in (("skin_type", "skin"), ("hair_type", "hair")):
            claim = record.get("attributes", {}).get(name)
            if not claim or not isinstance(claim["value"], str): continue
            value = claim["value"]
            if len(value) <= 80 and re.fullmatch(r"[\w -]+", value) and re.search(
                    r"\b" + re.escape(value) + r"\s+" + noun + r"\b", concern, re.I):
                item["evidence"].append({"field": claim["field"], "text": value, "status": "listing_excerpt", "attribute": name})
                item["topics"].append(name.replace("_", " "))
        topics = [(pattern, label) for trigger, pattern, label in TOPICS if re.search(trigger, concern, re.I)]
        # Exact phrases remain useful for fashion/materials and unlisted concerns.
        phrase = " ".join(concern.split())
        if len(phrase) >= 3:
            topics.append((r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)", "exact wording"))
        for pattern, label in topics:
            for field, text in fields:
                match = re.search(pattern, text, re.I)
                if not match or match.end() - match.start() > 400:
                    continue
                # Keep the clause, including negations. A mention is never a positive claim.
                start = max(0, match.start() - 100)
                while start > 0 and not text[start - 1].isspace() and match.start() - start < 130:
                    start -= 1
                end = min(len(text), max(start + 320, match.end() + 100))
                if end - start > 600: end = start + 600
                excerpt = {"field": field, "text": text[start:end], "status": "listing_excerpt"}
                if label not in item["topics"]: item["topics"].append(label)
                if excerpt not in item["evidence"] and len(item["evidence"]) < 2:
                    item["evidence"].append(excerpt)
                break
        if item["evidence"]: item["status"] = "listing_mention"
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
            reasons.append(prefix + "related listing wording — " + quotes + " These mentions do not explicitly confirm suitability or results for you.")
        else:
            reasons.append(prefix + "the available listing fields do not explicitly confirm suitability; no matching evidence was found.")
    return reasons

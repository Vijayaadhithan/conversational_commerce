"""Replies for validated shopping conversations."""

from copy import deepcopy
from decimal import Decimal

from app.listing_explanations import concern_evidence, concern_reasons


def card_reply(products, compare=False):
    lines = []
    for index, row in enumerate(products, 1):
        lines.append(
            f"{row.get('display_position', index)}. {row['title']} — ${row['price']['amount']} USD (historical)."
        )
        for reason in row.get("why_recommended", []):
            lines.append("   " + reason)
    if compare and len(products) > 1:
        cheapest = min(products, key=lambda row: Decimal(row["price"]["amount"]))
        highest = max(Decimal(row["price"]["amount"]) for row in products)
        difference = highest - Decimal(cheapest["price"]["amount"])
        lines.append(
            f"Lowest historical price: {cheapest['title']}; difference from the highest: ${difference:.2f} USD."
        )
        lines.append(
            "Missing attributes remain unknown; the listings do not establish which product will work better for you."
        )
    return "\n".join(lines)


def explain_products(rows, task):
    products = deepcopy(rows)
    for position, row in enumerate(products, 1):
        reasons = []
        if task.filters.max_price is not None:
            reasons.append(
                f"Its listed historical price is within your ${task.filters.max_price:.2f} maximum."
            )
        for name in task.filters.attributes:
            claim = row["attributes"].get(name)
            if claim:
                reasons.append(
                    f"The listing explicitly states {name.replace('_', ' ')}: {claim['value']} ({claim['field']})."
                )
        concerns = task_concerns(task)
        evidence = row.get("concern_evidence")
        if evidence is None or [item["concern"] for item in evidence] != concerns:
            evidence = concern_evidence(row, concerns)
        reasons.extend(concern_reasons(evidence))
        for caution in row.get("listing_quality", {}).get("cautions", []):
            sources = [caution["evidence"], *caution.get("other_evidence", [])]
            quotes = " ".join(f"“{source['text']}” ({source['field']})." for source in sources)
            reasons.append(caution["text"] + " Listing: " + quotes)
        if not concerns:
            snippets = row.get("listing_evidence", [])
            if snippets:
                for snippet in snippets[:2]:
                    reasons.append(
                        f"Related listing text: “{snippet['text']}” ({snippet['field']})."
                    )
            else:
                reasons.append(
                    "Retrieved for similarity to your product request; the listing does not provide an exact matching excerpt."
                )
        row.update(display_position=position, why_recommended=reasons, concern_evidence=evidence)
    return products


def task_concerns(task):
    return (
        list(dict.fromkeys(item.value for item in task.needs))
        if task.needs
        else list(dict.fromkeys(part.strip() for part in task.need.split(";") if part.strip()))[:20]
    )

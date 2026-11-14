"""Replies for validated shopping conversations."""

import re
from copy import deepcopy
from decimal import Decimal

from app.listing_explanations import concern_evidence, concern_reasons


def concise_reasons(row):
    """Render short listing evidence without dropping its uncertainty or negation."""
    reasons = []
    if row.get("listing_quality", {}).get("cautions"):
        reasons.append("This listing has cautions; check its detailed evidence before choosing.")
    labels = {
        "positive": "Related listing wording",
        "mixed": "Conflicting listing wording",
        "negated": "Listing warns against this requirement",
        "weak": "Only weak listing evidence",
    }
    concerns = sorted(
        row.get("concern_evidence", []),
        key=lambda item: {"mixed": 0, "negated": 0, "positive": 1, "weak": 2}.get(
            item.get("support"), 3
        ),
    )
    for item in concerns:
        # Do not truncate a long concern into a different requirement.
        if len(item["concern"]) > 120:
            reasons.append(
                "See the detailed evidence for your saved concern; suitability is unverified."
            )
            continue
        if item["status"] == "needs_clarification":
            label = "Preference needs clarification"
        else:
            label = labels.get(item.get("support"), "No matching listing evidence")
        if item.get("support") == "positive" and item.get("evidence"):
            excerpt = item["evidence"][0]["text"]
            reasons.append(
                f"Related listing wording for ‘{item['concern']}’: “{excerpt}”."
                if len(excerpt) <= 180
                else f"The listing includes wording related to ‘{item['concern']}’."
            )
        else:
            reasons.append(f"{label}: ‘{item['concern']}’. Suitability is unverified.")
    # Exact short attribute facts are preferable to repetitive budget statements.
    original = row.get("why_recommended", [])
    reasons.extend(
        re.sub(r"\s+\(/[^)]*\)(?=\.$)", "", reason)
        for reason in original
        if reason.startswith("The listing explicitly states ") and len(reason) <= 220
    )
    if not reasons:
        reasons.extend(reason for reason in original if len(reason) <= 220)
    return list(dict.fromkeys(reasons))


def recommendation_reply(task, products, plan):
    """Useful grounded wording remains available even when model writing fails."""
    if task.need:
        opening = f"With {task.need} in mind, here are {len(products)} options for {task.query}:"
    else:
        opening = f"Here are {len(products)} options for {task.query}:"
    if plan.feedback == "dislike":
        opening = "I've left out the options you rejected. " + opening
    elif plan.feedback == "like":
        opening = "I've saved the options you liked. " + opening
    if plan.intent in {"refine", "cheaper", "similar", "more"} or plan.feedback:
        constraints = []
        if task.filters.max_price is not None:
            constraints.append(f"maximum ${task.filters.max_price:.2f}")
        department = task.filters.attributes.get("department")
        if department:
            constraints.append(f"recipient: {department}")
        if constraints:
            opening += " Kept your " + " and ".join(constraints) + "."
    if task.need:
        incomplete = [
            concern
            for concern in task_concerns(task)
            if not all(
                any(
                    item.get("concern") == concern and item.get("support") == "positive"
                    for item in row.get("concern_evidence", [])
                )
                for row in products
            )
        ]
        if incomplete:
            opening += (
                " These are partial matches; not every listing confirms all your preferences."
            )
    return (
        opening
        + "\n"
        + card_reply(products, compact=True)
        + "\nPrices are historical USD; suitability and stock are unverified. "
        "You can explore an option or ask for a different style."
    )


def card_reply(products, compare=False, compact=False):
    lines = []
    for index, row in enumerate(products, 1):
        lines.append(
            f"{row.get('display_position', index)}. {row['title']} — ${row['price']['amount']} USD (historical)."
        )
        reasons = (
            [
                reason
                for reason in concise_reasons(row)
                if not reason.startswith(
                    ("No matching listing evidence", "Preference needs clarification")
                )
            ][:1]
            if compact
            else row.get("why_recommended", [])
        )
        for reason in reasons:
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

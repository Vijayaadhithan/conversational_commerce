"""Validate explicit reviewer labels against captured IDs and compute transparent counts."""
import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def summarize(capture, review):
    assert review["human_reviewed"] is False
    judgments = {case["id"]: case for case in review["cases"]}
    assert set(judgments) == {case["id"] for case in capture["cases"]}
    outcomes, grades = Counter(), Counter()
    http = Counter(); producing = 0; cards = 0; violations = []
    rows = []
    for case in capture["cases"]:
        judgment = judgments[case["id"]]
        assert type(judgment["outcome"]) is int and judgment["outcome"] in (0, 1, 2)
        outcomes[judgment["outcome"]] += 1
        recommendations = []
        for observed in case["observations"]:
            http[observed["http_status"]] += 1
            commerce = observed["response"].get("commerce", {})
            if commerce.get("action") == "recommend" and commerce.get("products"):
                recommendations.append(commerce)
            for group in commerce.get("groups", []) or [commerce]:
                filters = (group.get("search") or {}).get("filters_applied", {})
                for product in group.get("products", []):
                    cards += 1
                    amount = Decimal(product["price"]["amount"])
                    checks = []
                    for key in ("domain", "family"):
                        if filters.get(key): checks.append(product["category"][key] == filters[key])
                    if filters.get("max_price") is not None: checks.append(amount <= Decimal(filters["max_price"]))
                    if filters.get("min_price") is not None: checks.append(amount >= Decimal(filters["min_price"]))
                    for name, value in filters.get("attributes", {}).items():
                        claim = product["attributes"].get(name)
                        checks.append(bool(claim) and str(claim["value"]).casefold() == str(value).casefold())
                    if filters.get("brand"):
                        checks.append(bool(product.get("brand")) and product["brand"]["value"].casefold() == filters["brand"].casefold())
                    checks.append(product["product_id"] not in filters.get("exclude_product_ids", []))
                    if filters.get("exclude_brands"):
                        checks.append(bool(product.get("brand")) and product["brand"]["value"].casefold() not in [b.casefold() for b in filters["exclude_brands"]])
                    if not all(checks): violations.append({"case": case["id"], "product_id": product["product_id"]})
        producing += bool(recommendations)
        products = recommendations[-1]["products"] if recommendations else []
        assert set(judgment["product_grades"]) == {p["product_id"] for p in products}, case["id"]
        for grade in judgment["product_grades"].values():
            assert type(grade) is int and grade in (0, 1, 2)
            grades[grade] += 1
        rows.append({"id": case["id"], "outcome": judgment["outcome"], "returned_products": len(products), "note": judgment["note"]})
    total = sum(grades.values())
    return {"review_type": "AI-reviewed synthetic conversations", "human_reviewed": False,
        "conversations": len(capture["cases"]), "http_status_counts": dict(http),
        "conversation_outcome_counts": dict(outcomes), "conversations_with_recommendations": producing,
        "reviewed_product_grades": dict(grades), "reviewed_products": total,
        "strong_relevance_fraction": grades[2] / total if total else None,
        "at_least_partial_relevance_fraction": (grades[1] + grades[2]) / total if total else None,
        "returned_card_occurrences_checked": cards, "structured_filter_violations": violations,
        "limitation": "Constraint checks cover extracted filters, not omitted user requirements. Product judgments assess listing fit, not safety/efficacy. No corpus-wide recall or NDCG claim.",
        "cases": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True); parser.add_argument("--review", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = summarize(json.loads(Path(args.capture).read_text()), json.loads(Path(args.review).read_text()))
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != "cases"}, indent=2))

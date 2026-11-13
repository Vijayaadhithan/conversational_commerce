"""Mechanical conversation measures, kept separate from human quality judgments."""

import math
from collections import Counter, defaultdict
from statistics import median


def dialogue_metrics(cases):
    actions, generation, durations = Counter(), Counter(), []
    routes = Counter()
    stages, search_stages = defaultdict(list), defaultdict(list)
    products_ready = []
    errors = repairs = repeated = turns = product_turns = rejected_repeats = 0
    consecutive_questions, first_products = 0, []
    for case in cases:
        questions = set()
        question_streak, found_products = 0, False
        for turn_index, item in enumerate(case.get("observations", []), 1):
            turns += 1
            errors += item["http_status"] >= 400
            if valid_duration(item.get("seconds")):
                durations.append(item["seconds"])
            commerce = item.get("response", {}).get("commerce", {})
            products = commerce.get("products", [])
            if products:
                product_turns += 1
                if not found_products:
                    first_products.append(turn_index)
                    found_products = True
            preferences = commerce.get("preferences", {})
            rejected = {}
            for task in preferences.get("tasks", []):
                sentiments = {}
                for reaction in task.get("feedback", []):
                    sentiments.update(dict.fromkeys(reaction["product_ids"], reaction["sentiment"]))
                rejected[task["id"]] = {
                    key for key, value in sentiments.items() if value == "dislike"
                }
            rejected_repeats += sum(
                row.get("product_id")
                in rejected.get(row.get("task_id", preferences.get("active_task_id")), set())
                for row in products
            )
            routes[commerce.get("response_route", {}).get("mode", "not_reported")] += 1
            timing = commerce.get("timings", {})
            for field, samples in [
                ("stages_seconds", stages),
                ("search_stages_seconds", search_stages),
            ]:
                for stage, value in timing.get(field, {}).items():
                    if valid_duration(value):
                        samples[stage].append(value)
            if valid_duration(timing.get("products_ready_seconds")):
                products_ready.append(timing["products_ready_seconds"])
            actions[commerce.get("action", "error")] += 1
            generation[commerce.get("answer_generation", {}).get("status", "not_reported")] += 1
            repairs += bool(commerce.get("validation_recovered"))
            if commerce.get("action") == "clarify":
                question_streak += 1
                consecutive_questions += question_streak > 1
                question = " ".join(commerce.get("reply", "").casefold().split())
                repeated += bool(question and question in questions)
                if question:
                    questions.add(question)
            else:
                question_streak = 0
    durations.sort()
    return {
        "measurement_type": "mechanical_only_not_relevance_or_task_success",
        "turns": turns,
        "http_errors": errors,
        "validation_recoveries": repairs,
        "action_counts": dict(actions),
        "answer_generation_counts": dict(generation),
        "response_route_counts": dict(routes),
        "stage_latency_seconds": {
            stage: latency_summary(values) for stage, values in stages.items()
        },
        "search_stage_latency_seconds": {
            stage: latency_summary(values) for stage, values in search_stages.items()
        },
        "products_ready_latency_seconds": latency_summary(products_ready),
        "exact_repeated_clarifications": repeated,
        "consecutive_clarification_turns": consecutive_questions,
        "turns_with_products": product_turns,
        "first_products_turn": latency_summary(first_products),
        "rejected_product_repeats": rejected_repeats,
        "latency_seconds": {
            "median": round(median(durations), 4) if durations else None,
            "p95": round(durations[math.ceil(len(durations) * 0.95) - 1], 4) if durations else None,
        },
    }


def check_turn_expectations(response, expected):
    """Scenario assertions are observable behaviour, never a relevance judgment."""
    commerce = response.get("commerce", {})
    checks = {}
    if "action" in expected:
        checks["action"] = commerce.get("action") == expected["action"]
    if "products" in expected:
        checks["products"] = bool(commerce.get("products")) == expected["products"]
    if "search" in expected:
        checks["search"] = (
            bool(commerce.get("timings", {}).get("search_calls")) == expected["search"]
        )
    return {"passed": bool(commerce) and all(checks.values()), "checks": checks}


def valid_duration(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def latency_summary(values):
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median": round(median(ordered), 6) if ordered else None,
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 6) if ordered else None,
    }


def review_template(capture):
    """Unscored, capture-specific labels compatible with summarize_evaluation.py."""
    cases, seen = [], set()
    for case in capture["cases"]:
        if not isinstance(case.get("id"), str) or not case["id"].strip() or case["id"] in seen:
            raise ValueError("Capture case IDs must be nonblank and unique")
        seen.add(case["id"])
        products = []
        for turn in case.get("observations", []):
            commerce = turn.get("response", {}).get("commerce", {})
            if commerce.get("action") in {"recommend", "multi"} and commerce.get("products"):
                products = commerce["products"]
        cases.append(
            {
                "id": case["id"],
                "outcome": None,
                "note": "",
                "product_grades": {row["product_id"]: None for row in products},
            }
        )
    return {
        "reviewer": "",
        "human_reviewed": None,
        "instructions": "Set reviewer and human_reviewed explicitly. Review every turn for missed constraints, unsupported claims, unnecessary questions and task completion. Grade only the final nonempty recommendation products below. Fill every null before summarizing.",
        "rubric": {
            "0": "Poor fit or failed goal",
            "1": "Partial fit or unresolved requirement",
            "2": "Listing-supported fit or completed goal",
        },
        "cases": cases,
    }

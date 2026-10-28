"""Mechanical conversation measures, kept separate from human quality judgments."""

import math
from collections import Counter
from statistics import median


def dialogue_metrics(cases):
    actions, generation, durations = Counter(), Counter(), []
    errors = repairs = repeated = turns = 0
    for case in cases:
        questions = set()
        for item in case.get("observations", []):
            turns += 1
            errors += item["http_status"] >= 400
            if isinstance(item.get("seconds"), (int, float)):
                durations.append(item["seconds"])
            commerce = item.get("response", {}).get("commerce", {})
            actions[commerce.get("action", "error")] += 1
            generation[commerce.get("answer_generation", {}).get("status", "not_reported")] += 1
            repairs += bool(commerce.get("validation_recovered"))
            if commerce.get("action") == "clarify":
                question = " ".join(commerce.get("reply", "").casefold().split())
                repeated += bool(question and question in questions)
                if question:
                    questions.add(question)
    durations.sort()
    return {
        "measurement_type": "mechanical_only_not_relevance_or_task_success",
        "turns": turns,
        "http_errors": errors,
        "validation_recoveries": repairs,
        "action_counts": dict(actions),
        "answer_generation_counts": dict(generation),
        "exact_repeated_clarifications": repeated,
        "latency_seconds": {
            "median": round(median(durations), 4) if durations else None,
            "p95": round(durations[math.ceil(len(durations) * 0.95) - 1], 4) if durations else None,
        },
    }

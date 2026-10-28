import importlib.util
from pathlib import Path

import pytest

from app.conversation_evaluation import dialogue_metrics


def test_dialogue_capture_metrics_are_not_task_success_or_relevance():
    clarification = {
        "http_status": 200,
        "seconds": 2,
        "response": {
            "commerce": {
                "action": "clarify",
                "reply": "Which option?",
                "answer_generation": {"status": "fallback"},
            }
        },
    }
    metrics = dialogue_metrics(
        [
            {
                "observations": [
                    clarification,
                    clarification,
                    {
                        "http_status": 503,
                        "seconds": 8,
                        "response": {"detail": "unavailable"},
                    },
                ]
            }
        ]
    )
    assert metrics["turns"] == 3 and metrics["http_errors"] == 1
    assert metrics["exact_repeated_clarifications"] == 1
    assert metrics["answer_generation_counts"]["fallback"] == 2
    assert metrics["latency_seconds"] == {"median": 2, "p95": 8}
    assert "not_relevance_or_task_success" in metrics["measurement_type"]
    assert dialogue_metrics([])["latency_seconds"]["p95"] is None


spec = importlib.util.spec_from_file_location(
    "summarize_evaluation", Path(__file__).resolve().parents[1] / "scripts/summarize_evaluation.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_http_success_is_not_counted_as_conversation_success():
    capture = {
        "cases": [
            {
                "id": "clear_request",
                "observations": [
                    {
                        "http_status": 200,
                        "response": {"commerce": {"action": "clarify", "products": []}},
                    }
                ],
            }
        ]
    }
    review = {
        "human_reviewed": False,
        "cases": [
            {
                "id": "clear_request",
                "outcome": 0,
                "product_grades": {},
                "note": "Unnecessary clarification",
            }
        ],
    }
    result = module.summarize(capture, review)
    assert result["http_status_counts"] == {200: 1}
    assert result["conversation_outcome_counts"] == {0: 1}
    assert result["strong_relevance_fraction"] is None
    review["cases"][0]["product_grades"] = {"invented-product-id": 2}
    with pytest.raises(ValueError, match="Product grade IDs"):
        module.summarize(capture, review)


def test_multi_group_cards_and_department_aliases_are_counted():
    product = {
        "product_id": "p1",
        "price": {"amount": "10"},
        "attributes": {"department": {"value": "Women's"}},
        "category": {},
    }
    group = {
        "products": [product],
        "search": {"filters_applied": {"attributes": {"department": "womens"}}},
    }
    capture = {
        "cases": [
            {
                "id": "multiple",
                "observations": [
                    {
                        "http_status": 200,
                        "response": {
                            "commerce": {
                                "action": "multi",
                                "products": [product],
                                "groups": [group],
                            }
                        },
                    }
                ],
            }
        ]
    }
    review = {
        "human_reviewed": False,
        "cases": [
            {
                "id": "multiple",
                "outcome": 2,
                "product_grades": {"p1": 2},
                "note": "Supported literal department alias",
            }
        ],
    }
    result = module.summarize(capture, review)
    assert result["conversations_with_recommendations"] == 1
    assert result["returned_card_occurrences_checked"] == 1
    assert not result["structured_filter_violations"]


def empty_capture_and_review():
    return (
        {"cases": [{"id": "one", "observations": []}]},
        {
            "human_reviewed": False,
            "cases": [{"id": "one", "outcome": 0, "product_grades": {}, "note": "No results"}],
        },
    )


def test_review_attribution_and_duplicate_case_validation():
    from copy import deepcopy

    capture, review = empty_capture_and_review()
    review["human_reviewed"] = True
    result = module.summarize(capture, review)
    assert result["human_reviewed"] is True
    assert result["review_type"] == "Human-reviewed synthetic conversations"
    assert result["reviewed_product_scope"] == "final recommendation turn per conversation"
    for data in [capture, review]:
        original = deepcopy(data["cases"])
        data["cases"] += deepcopy(original)
        with pytest.raises(ValueError, match="unique"):
            module.summarize(capture, review)
        data["cases"] = original
    review["human_reviewed"] = "true"
    with pytest.raises(ValueError, match="human_reviewed"):
        module.summarize(capture, review)


def test_mislabeled_scope_is_checked_against_conversation_not_just_returned_filters():
    capture, review = empty_capture_and_review()
    capture["cases"][0].update(
        storefront_id="beauty",
        observations=[
            {
                "http_status": 200,
                "response": {
                    "commerce": {
                        "action": "recommend",
                        "products": [
                            {
                                "product_id": "p1",
                                "price": {"amount": "10"},
                                "attributes": {},
                                "category": {"family": "apparel", "domain": "fashion"},
                            }
                        ],
                        "search": {"filters_applied": {"storefront_id": "clothing"}},
                    }
                },
            }
        ],
    )
    review["cases"][0]["product_grades"] = {"p1": 0}
    result = module.summarize(capture, review)
    assert result["structured_filter_violations"] == [{"case": "one", "product_id": "p1"}]


def test_invalid_review_is_rejected_with_python_optimization_enabled():
    import subprocess
    import sys

    code = """
from scripts.summarize_evaluation import summarize
try:
    summarize({'cases':[]}, {'human_reviewed':'false', 'cases':[]})
except ValueError:
    pass
else:
    raise RuntimeError('Invalid review was accepted')
"""
    result = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", [True, -1, 3, "2"])
def test_invalid_grades_fail_explicitly(value):
    capture, review = empty_capture_and_review()
    review["cases"][0]["outcome"] = value
    with pytest.raises(ValueError, match="integer"):
        module.summarize(capture, review)

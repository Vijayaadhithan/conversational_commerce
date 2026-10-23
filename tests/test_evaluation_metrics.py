import importlib.util
from pathlib import Path

import pytest

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
    with pytest.raises(AssertionError):
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

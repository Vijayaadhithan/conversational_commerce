import importlib.util
from pathlib import Path

import pytest

from app.conversation_evaluation import dialogue_metrics, review_template


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


def test_stage_percentiles_have_sample_counts_and_ignore_old_provenance():
    cases = [
        {
            "observations": [
                {
                    "http_status": 200,
                    "seconds": 3,
                    "response": {
                        "commerce": {
                            "action": "details",
                            "response_route": {"mode": "fast"},
                            "search": {"timings": {"embedding_seconds": 100}},
                            "timings": {
                                "stages_seconds": {"intent_seconds": 0.2},
                                "search_stages_seconds": {},
                                "products_ready_seconds": 0.3,
                            },
                        }
                    },
                }
            ]
        }
    ]
    result = dialogue_metrics(cases)
    assert result["response_route_counts"] == {"fast": 1}
    assert result["stage_latency_seconds"]["intent_seconds"] == {
        "samples": 1,
        "median": 0.2,
        "p95": 0.2,
    }
    assert result["search_stage_latency_seconds"] == {}
    assert result["products_ready_latency_seconds"]["median"] == 0.3


def test_review_template_uses_last_recommendation_ids_and_cannot_be_scored_unfilled():
    capture = {
        "cases": [
            {
                "id": "one",
                "observations": [
                    {
                        "response": {
                            "commerce": {"action": "recommend", "products": [{"product_id": "old"}]}
                        }
                    },
                    {
                        "response": {
                            "commerce": {"action": "multi", "products": [{"product_id": "new"}]}
                        }
                    },
                    {
                        "response": {
                            "commerce": {"action": "details", "products": [{"product_id": "old"}]}
                        }
                    },
                ],
            }
        ]
    }
    review = review_template(capture)
    assert review["cases"][0]["product_grades"] == {"new": None}
    assert review["human_reviewed"] is None and review["cases"][0]["outcome"] is None
    with pytest.raises(ValueError, match="human_reviewed"):
        module.summarize(capture, review)


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


def test_browsing_metrics_report_first_products_question_loops_and_rejected_repeats():
    question = {
        "http_status": 200,
        "response": {"commerce": {"action": "clarify", "reply": "Which style?"}},
    }
    products = {
        "http_status": 200,
        "response": {
            "commerce": {
                "action": "recommend",
                "products": [{"product_id": "p1"}],
                "preferences": {
                    "active_task_id": 1,
                    "tasks": [
                        {"id": 1, "feedback": [{"sentiment": "dislike", "product_ids": ["p1"]}]}
                    ],
                },
            }
        },
    }
    metrics = dialogue_metrics([{"observations": [question, question, products]}])
    assert metrics["turns_with_products"] == 1
    assert metrics["first_products_turn"]["median"] == 3
    assert metrics["consecutive_clarification_turns"] == 1
    assert metrics["rejected_product_repeats"] == 1


def test_scenario_expectations_check_behaviour_without_scoring_relevance():
    from app.conversation_evaluation import check_turn_expectations

    response = {
        "commerce": {
            "action": "recommend",
            "products": [{"product_id": "p1"}],
            "timings": {"search_calls": 1},
        }
    }
    assert check_turn_expectations(response, {"products": True, "search": True})["passed"]
    assert not check_turn_expectations(response, {"search": False})["passed"]
    assert not check_turn_expectations({"detail": "failed"}, {})["passed"]


def test_demo_expectations_detect_lost_budget_and_criticism_saved_as_need():
    from copy import deepcopy

    from app.conversation_evaluation import check_turn_expectations

    response = {
        "commerce": {
            "products": [{"product_id": "p2"}],
            "preferences": {
                "active_task_id": 1,
                "tasks": [
                    {
                        "id": 1,
                        "query": "jacket",
                        "need": "",
                        "filters": {"max_price": "59.99", "attributes": {"department": "mens"}},
                    }
                ],
            },
        }
    }
    previous = deepcopy(response)
    previous["commerce"]["products"] = [{"product_id": "p1"}]
    expected = {
        "products_changed": True,
        "task_count": 1,
        "active_filters": {"max_price": "59.99"},
        "need_excludes": ["too formal"],
        "constraints_preserved": ["max_price", "attributes.department"],
        "task_filters": [{"query": "jacket", "filters": {"max_price": "59.99"}}],
    }
    assert check_turn_expectations(response, expected, previous)["passed"]
    task = response["commerce"]["preferences"]["tasks"][0]
    task["filters"]["max_price"] = None
    task["need"] = "too formal"
    checks = check_turn_expectations(response, expected, previous)["checks"]
    assert not checks["constraints_preserved"] and not checks["need_excludes"]
    assert not checks["active_filters"] and not checks["task_filters"]
    with pytest.raises(ValueError, match="Unknown scenario"):
        check_turn_expectations(response, {"typo": True})

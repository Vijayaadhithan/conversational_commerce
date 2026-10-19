import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("summarize_evaluation", Path(__file__).resolve().parents[1] / "scripts/summarize_evaluation.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_http_success_is_not_counted_as_conversation_success():
    capture = {"cases": [{"id": "clear_request", "observations": [{"http_status": 200,
        "response": {"commerce": {"action": "clarify", "products": []}}}]}]}
    review = {"human_reviewed": False, "cases": [{"id": "clear_request", "outcome": 0,
        "product_grades": {}, "note": "Unnecessary clarification"}]}
    result = module.summarize(capture, review)
    assert result["http_status_counts"] == {200: 1}
    assert result["conversation_outcome_counts"] == {0: 1}
    assert result["strong_relevance_fraction"] is None
    review["cases"][0]["product_grades"] = {"invented-product-id": 2}
    with pytest.raises(AssertionError): module.summarize(capture, review)

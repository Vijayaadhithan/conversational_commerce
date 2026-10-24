"""Unknown or malformed provider counts must not produce plausible partial totals."""

import pytest

from app.inference_usage import aggregate_usage


def test_combines_attempts_and_keeps_fields_independent():
    assert aggregate_usage(
        [
            {"prompt_eval_count": 10, "eval_count": 3},
            {"prompt_eval_count": 5, "eval_count": 0},
        ]
    ) == {"prompt_tokens": 15, "completion_tokens": 3}
    assert aggregate_usage(
        [
            {"prompt_eval_count": 10, "eval_count": 3},
            {"eval_count": 2},
        ]
    ) == {"prompt_tokens": None, "completion_tokens": 5}


@pytest.mark.parametrize("count", [None, True, -1, "10", 1.5])
def test_invalid_counts_stay_unknown(count):
    assert aggregate_usage([{"prompt_eval_count": count, "eval_count": 0}]) == {
        "prompt_tokens": None,
        "completion_tokens": 0,
    }


def test_absent_usage_is_not_reported_as_zero():
    assert (
        aggregate_usage([])
        == aggregate_usage([{}])
        == {
            "prompt_tokens": None,
            "completion_tokens": None,
        }
    )

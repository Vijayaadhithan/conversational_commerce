"""Aggregate provider token usage without presenting unknown counts as zero."""

from collections.abc import Mapping, Sequence


def aggregate_usage(results: Sequence[Mapping[str, object]]) -> dict[str, int | None]:
    usage = {}
    for output, field in (
        ("prompt_tokens", "prompt_eval_count"),
        ("completion_tokens", "eval_count"),
    ):
        counts = [result.get(field) for result in results]
        usage[output] = (
            sum(counts)
            if counts and all(type(count) is int and count >= 0 for count in counts)
            else None
        )
    return usage

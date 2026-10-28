"""Per-turn stage timing and conservative routing for code-rendered answers."""

import math
import time
from contextlib import contextmanager

from app.commerce_intent import direct_plan

SEARCH_STAGES = {
    "setup_cache_seconds",
    "eligibility_seconds",
    "embedding_seconds",
    "branches_seconds",
    "hydration_seconds",
    "reranking_seconds",
    "postprocess_seconds",
}


class StageTimer:
    def __init__(self, clock=None):
        self.clock = clock or time.perf_counter
        self.started = self.clock()
        self.seconds = {}

    @contextmanager
    def measure(self, stage):
        started = self.clock()
        try:
            yield
        finally:
            self.seconds[stage] = self.seconds.get(stage, 0.0) + self.clock() - started

    def elapsed(self):
        return self.clock() - self.started

    def snapshot(self):
        return {name: round(value, 6) for name, value in self.seconds.items()}


def response_route(settings, message, commerce):
    if not settings.commerce_natural:
        return {"mode": "legacy", "reason": "natural_replies_disabled"}
    if (
        settings.commerce_fast_path
        and direct_plan(message) is not None
        and not commerce.get("groups")
        and not commerce.get("validation_recovered")
        and not commerce.get("policy_redirect")
        and commerce["action"] in {"recommend", "details", "compare", "preferences", "smalltalk"}
    ):
        return {"mode": "fast", "reason": "deterministic_command"}
    return {"mode": "reviewed", "reason": "natural_reply_policy"}


def accumulate_search_stages(total, search):
    # Only called for a search executed in this turn. Inspection provenance and
    # timings persisted with old product snapshots are not current search work.
    for name, value in search.get("timings", {}).items():
        if (
            name in SEARCH_STAGES
            and type(value) in (int, float)
            and math.isfinite(value)
            and value >= 0
        ):
            total[name] = total.get(name, 0.0) + value

"""Fast routing preserves validated results, while timing measures only current work."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, harness, new
from test_commerce_feedback import seeded
from test_commerce_stream import frames

from app.commerce_performance import StageTimer, accumulate_search_stages, response_route


def test_stage_timer_accumulates_failed_and_retried_work():
    clock = iter([0, 1, 3, 4, 7])
    timer = StageTimer(clock=lambda: next(clock))
    with pytest.raises(ValueError), timer.measure("intent_seconds"):
        raise ValueError("invalid proposal")
    with timer.measure("intent_seconds"):
        pass
    assert timer.snapshot() == {"intent_seconds": 5}


@pytest.mark.parametrize(
    "message,action,fast",
    [
        ("hello", "smalltalk", True),
        ("Compare the first two", "compare", True),
        ("details about the second", "details", True),
        ("Make it $10", "recommend", True),
        ("More options", "recommend", True),
        ("Why is the second better for me?", "answer", False),
        ("Make it $10 and explain the tradeoffs", "recommend", False),
        ("moisturizer for sensitive skin", "recommend", False),
    ],
)
def test_only_complete_deterministic_commands_take_fast_path(message, action, fast):
    settings = SimpleNamespace(commerce_natural=True, commerce_fast_path=True)
    value = {"action": action}
    assert (response_route(settings, message, value)["mode"] == "fast") is fast
    settings.commerce_fast_path = False
    assert response_route(settings, message, value)["mode"] == "reviewed"
    settings.commerce_natural = False
    assert response_route(settings, message, value)["mode"] == "legacy"


def test_search_stage_aggregation_is_bounded_and_rejects_non_finite_values():
    total = {}
    accumulate_search_stages(
        total, {"timings": {"embedding_seconds": 2, "arbitrary": 100, "total_seconds": 4}}
    )
    accumulate_search_stages(
        total,
        {
            "timings": {
                "embedding_seconds": 3,
                "branches_seconds": float("nan"),
                "hydration_seconds": -1,
            }
        },
    )
    assert total == {"embedding_seconds": 5}


def test_fast_details_skip_inference_and_do_not_count_old_search_timings(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMERCE_NATURAL_REPLIES", "1")
    _, app, calls = harness(tmp_path, {})
    state, _ = seeded()
    state.active().last_search["timings"] = {"embedding_seconds": 99}
    with TestClient(app) as client:
        cid = new(client)
        app.state.store.commit_commerce_turn(
            cid, "moisturizer", state, {"reply": "Saved options"}, 0
        )
        url = f"/conversations/{cid}/messages"
        headers = {"Idempotency-Key": "fast-details", "Accept": "text/event-stream"}
        events = frames(
            client.post(url, json={"message": "details about the second"}, headers=headers).text
        )
        assert events[-1][0] == "done", events
        result = events[-1][1]
        value = result["commerce"]
        assert value["response_route"]["mode"] == "fast" and calls == []
        assert value["products"][0]["display_position"] == 2
        assert value["search"]["timings"]["embedding_seconds"] == 99
        assert value["timings"]["search_calls"] == 0
        assert value["timings"]["search_stages_seconds"] == {}
        assert "generation_seconds" not in value["timings"]["stages_seconds"]
        assert "commit_seconds" not in value["timings"]["stages_seconds"]
        assert (
            value["timings"]["before_commit_seconds"] >= value["timings"]["products_ready_seconds"]
        )
        assert "".join(data["text"] for name, data in events if name == "delta") == result["reply"]
        replay = client.post(url, json={"message": "details about the second"}, headers=headers)
        assert frames(replay.text)[-1][1] == result
        assert client.get(url).json()["messages"][-1]["commerce"] == value
        assert app.state.metrics.counts["turn_route_fast_total"] == 1


def test_fast_budget_refinement_still_searches_with_exact_saved_constraints(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMERCE_NATURAL_REPLIES", "1")
    _, app, calls = harness(tmp_path, {})
    state, _ = seeded()
    state.active().filters.brand = "Acme"

    class TimedSearch(Search):
        def search(self, request):
            result = super().search(request)
            result["timings"] = {"embedding_seconds": 0.01, "reranking_seconds": 0.02}
            return result

    with TestClient(app) as client:
        app.state.product_search = search = TimedSearch()
        cid = new(client)
        app.state.store.commit_commerce_turn(
            cid, "moisturizer", state, {"reply": "Saved options"}, 0
        )
        response = client.post(f"/conversations/{cid}/messages", json={"message": "Make it $10"})
        assert response.status_code == 200, response.text
        value = response.json()["commerce"]
        assert value["response_route"]["mode"] == "fast" and calls == []
        assert search.calls[-1].filters.max_price == 10 and search.calls[-1].filters.brand == "Acme"
        assert value["timings"]["search_calls"] == 1
        assert value["timings"]["search_stages_seconds"]["reranking_seconds"] == 0.02

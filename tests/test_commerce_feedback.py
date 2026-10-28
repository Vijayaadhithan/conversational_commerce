"""Stateful product reactions, reference resolution and combined follow-ups."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, commerce, harness, new, plan

from app.commerce import handle_turn
from app.commerce_errors import IntentError
from app.commerce_feedback import rejected_ids
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.main import create_app
from app.search_models import ProductFilters


def seeded():
    search = Search()
    state, _ = handle_turn(
        {}, IntentPlan(intent="search", evidence="moisturizer"), "moisturizer", search.search
    )
    return state, search


def turn(state, search, message, intent, **kwargs):
    return handle_turn(
        state.model_dump(),
        IntentPlan(intent=intent, evidence=message, **kwargs),
        message,
        search.search,
    )


def test_answer_uses_inspected_reference_without_search_or_new_preferences():
    state, search = seeded()
    state, _ = turn(state, search, "details about the second", "details")
    filters = state.active().filters.model_dump()
    state, result = turn(
        state, search, "Does it help with dry skin?", "answer", recommendation_requested=False
    )
    assert result["action"] == "answer" and result["products"][0]["display_position"] == 2
    assert len(search.calls) == 1 and state.active().need == ""
    assert state.active().filters.model_dump() == filters


@pytest.mark.parametrize(
    "message", ["Why that one?", "Details about option 99", "Tell me about the sixth"]
)
def test_ambiguous_or_invalid_reference_never_uses_model_positions(message):
    state, search = seeded()
    state, result = turn(state, search, message, "answer", positions=[1])
    assert result["action"] == "clarify" and not result["products"] and len(search.calls) == 1


def test_similar_but_cheaper_uses_one_inspected_price_and_preserves_filters():
    state, search = seeded()
    state.active().filters.max_price = Decimal("20")
    state, _ = turn(state, search, "details about the third", "details")
    reference = state.active().shown[2]
    state, result = turn(
        state, search, "same style but cheaper", "similar", cheaper_than_reference=True
    )
    request = search.calls[-1]
    assert request.query == reference["title"]
    assert request.filters.max_price == Decimal(reference["price"]["amount"]) - Decimal("0.01")
    assert reference["product_id"] in request.filters.exclude_product_ids
    assert result["products"] and state.active().focused_positions == []
    assert "style" not in request.filters.attributes  # similarity is not a new mandatory claim


def test_single_digit_budget_is_not_a_product_reference():
    state, search = seeded()
    state, _ = turn(state, search, "details about the second", "details")
    reference = state.active().shown[1]
    _, _ = turn(state, search, "same style under $5", "similar")
    assert search.calls[-1].query == reference["title"]


@pytest.mark.parametrize(
    "reaction", ["I don't like these; too floral", "I do not like these; too floral"]
)
def test_rejection_is_literal_and_task_scoped_then_excluded_from_future_search(reaction):
    state, search = seeded()
    state.tasks.append(ShoppingTask(id=2, query="shampoo", filters=ProductFilters(max_price="30")))
    original_ids = [row["product_id"] for row in state.active().shown]
    state, result = turn(state, search, reaction, "feedback", feedback="dislike")
    assert result["action"] == "clarify" and len(search.calls) == 1
    assert rejected_ids(state.active()) == original_ids
    assert state.active().feedback[-1].evidence == reaction
    assert state.tasks[1].feedback == [] and state.active().filters.attributes == {}
    state, result = turn(state, search, "show options", "search")
    assert set(original_ids) <= set(search.calls[-1].filters.exclude_product_ids)
    assert not set(original_ids) & {row["product_id"] for row in result["products"]}
    assert state.tasks[1].filters.max_price == Decimal("30")


def test_explicit_like_can_reverse_a_rejection_without_losing_its_history():
    state, search = seeded()
    state, _ = turn(state, search, "I dislike the first", "feedback", feedback="dislike")
    state, _ = turn(state, search, "Actually I like the first", "feedback", feedback="like")
    assert len(state.active().feedback) == 2 and rejected_ids(state.active()) == []


@pytest.mark.parametrize(
    "message,sentiment",
    [
        ("I like the first", "dislike"),
        ("I dislike the first but like the second", "dislike"),
        ("I don't dislike the first", "dislike"),
        ("The first is not too floral", "dislike"),
    ],
)
def test_invalid_or_mixed_feedback_never_changes_saved_state(message, sentiment):
    state, search = seeded()
    before = state.model_dump()
    with pytest.raises(IntentError):
        turn(state, search, message, "feedback", feedback=sentiment)
    assert state.model_dump() == before and len(search.calls) == 1


def test_feedback_and_focus_survive_restart_and_reset_clears_them(tmp_path):
    first, reaction = "moisturizer", "I dislike the second"
    config, app, _ = harness(
        tmp_path,
        {first: plan("search", first), reaction: plan("feedback", reaction, feedback="dislike")},
    )
    with TestClient(app) as client:
        app.state.product_search = Search()
        cid = new(client)
        commerce(client, cid, first)
        commerce(client, cid, reaction)
    with TestClient(create_app(config)) as client:
        task = client.get(f"/conversations/{cid}/preferences").json()["preferences"]["tasks"][0]
        assert task["focused_positions"] == [2] and task["feedback"][0]["evidence"] == reaction
        assert commerce(client, cid, "Start over")["preferences"]["tasks"] == []


def test_legacy_state_still_loads_without_new_memory_fields():
    task = ShoppingState.model_validate(
        {"tasks": [{"id": 1, "query": "shampoo"}], "active_task_id": 1}
    ).active()
    assert task.feedback == [] and task.focused_positions == []


def test_reason_followup_belongs_to_the_prior_rejected_group():
    state, search = seeded()
    state, _ = turn(state, search, "I dislike these", "feedback", feedback="dislike")
    ids = state.active().feedback[-1].product_ids
    state, _ = turn(state, search, "too floral", "feedback", feedback="dislike")
    assert state.active().feedback[-1].evidence == "too floral"
    assert state.active().feedback[-1].product_ids == ids
    assert state.active().filters.attributes == {} and len(search.calls) == 1


def test_product_question_never_creates_tasks_or_searches():
    state, search = seeded()
    state, _ = turn(
        state, search, "What is the difference between shampoo and conditioner?", "answer"
    )
    assert len(state.tasks) == 1 and len(search.calls) == 1


def test_shared_reaction_with_multiple_requests_clarifies_without_partial_tasks():
    state, search = seeded()
    message = "I dislike these; shampoo under $15 and moisturizer under $20"
    updated, result = turn(state, search, message, "refine", feedback="dislike")
    assert result["action"] == "clarify" and updated.pending_kind == "task_selection"
    assert len(updated.tasks) == 1 and updated.active().feedback == [] and len(search.calls) == 1


def test_reaction_is_not_transferred_into_a_new_task():
    state, search = seeded()
    updated, result = turn(
        state, search, "I dislike these; now shampoo", "new_task", feedback="dislike"
    )
    assert result["action"] == "clarify" and updated.pending_kind == "task_selection"
    assert len(updated.tasks) == 1 and updated.active().feedback == [] and len(search.calls) == 1

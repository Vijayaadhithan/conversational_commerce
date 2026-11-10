"""Recipient follow-ups preserve literal evidence and never repeat a resolved question."""

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, change, commerce, harness, new, plan

from app.commerce import handle_turn
from app.commerce_intent import recipient_plan
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.commerce_recipients import recipient_evidence
from app.search_models import ProductFilters


@pytest.mark.parametrize(
    "answer,department",
    [
        ("adult man", "mens"),
        ("adult men", "mens"),
        ("  Adult MAN  ", "mens"),
        ("for an adult man", "mens"),
        ("adult woman", "womens"),
        ("adult women", "womens"),
        ("male", "mens"),
        ("female", "womens"),
        ("boy", "boys"),
        ("boys", "boys"),
        ("girl", "girls"),
        ("girls", "girls"),
    ],
)
def test_hiking_shoe_recipient_answer_searches_without_another_model_call(
    tmp_path, monkeypatch, answer, department
):
    monkeypatch.setenv("COMMERCE_NATURAL_REPLIES", "1")
    initial = "shoe for hiking"
    _, app, calls = harness(
        tmp_path,
        {
            initial: plan(
                "search", initial, change("query", initial, initial), recommendation_requested=True
            )
        },
    )

    class ShoeSearch(Search):
        def search(self, request):
            response = super().search(request.model_copy(update={"filters": ProductFilters()}))
            self.calls[-1] = request
            response["filters_applied"] = request.filters.model_dump(mode="json")
            for row in response["results"]:
                row["title"] = "Hiking shoe"
                row["attributes"]["department"] = {"value": department, "field": "/title"}
            return response

    with TestClient(app) as client:
        app.state.product_search = search = ShoeSearch()
        cid = new(client)
        first = commerce(client, cid, initial)
        assert first["action"] == "clarify" and first["preferences"]["pending_kind"] == "recipient"
        assert len(calls) == 1 and not search.calls
        result = commerce(client, cid, answer)
        assert result["action"] == "recommend" and result["products"]
        assert result["preferences"]["pending_kind"] != "recipient"
        assert "Who are these for" not in result["reply"]
        assert search.calls[-1].filters.attributes["department"] == department
        assert len(calls) == 1
        assert result["answer_generation"]["status"] == "skipped"
        saved = client.get(f"/conversations/{cid}/preferences").json()
        assert saved["preferences"]["tasks"][0]["filters"]["attributes"]["department"] == department


@pytest.mark.parametrize(
    "answer", ["not a man", "adult men or adult women", "man and boy", "man or child", "man-made"]
)
def test_ambiguous_or_negated_recipient_is_not_inferred(answer):
    assert recipient_evidence(answer) is None


def test_model_recipient_paraphrase_preserves_the_users_singular_wording():
    saved = ShoppingState(
        tasks=[
            ShoppingTask(
                id=1, query="shoe", filters=ProductFilters(family="footwear", max_price="75")
            )
        ],
        active_task_id=1,
        pending_kind="recipient",
    )
    message = "adult man"
    proposal = IntentPlan.model_validate(
        plan(
            "refine",
            message,
            change("recipient", "adult men", message),
            change("attribute.department", "mens", message),
            recommendation_requested=True,
        )
    )
    state, result = handle_turn(saved.model_dump(), proposal, message, Search().search)
    assert result["action"] == "recommend"
    assert state.active().recipient == message
    assert state.active().sources["recipient"]["evidence"] == message
    assert state.active().filters.max_price == 75


@pytest.mark.parametrize("message", ["adult man under $30", "not a man", "adult men or women"])
def test_recipient_shortcut_does_not_drop_additional_requirements(message):
    state = ShoppingState(
        tasks=[ShoppingTask(id=1, query="shoe", filters=ProductFilters(family="footwear"))],
        active_task_id=1,
        pending_kind="recipient",
    )
    assert recipient_plan(state, message) is None


def test_recipient_shortcut_requires_the_saved_recipient_question():
    state = ShoppingState(tasks=[ShoppingTask(id=1, query="shoe")], active_task_id=1)
    assert recipient_plan(state, "adult man") is None

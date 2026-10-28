"""Dialogue context stays bounded and cannot become new preference evidence."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, change, commerce, harness, new, plan

from app.chat_service import ChatService
from app.commerce import handle_turn
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.commerce_replies import card_reply, concise_reasons, explain_products
from app.commerce_summary import add_summary
from app.config import Settings
from app.main import create_app
from app.search_models import ProductFilters, ProductSearchRequest
from app.text_budget import byte_size


def test_recent_dialogue_survives_restart_is_scoped_and_stops_at_reset(tmp_path):
    first, second = "I have dry skin", "Could I browse now?"
    config, app, calls = harness(
        tmp_path,
        {
            first: plan("clarify", first, change("need", "dry skin", "dry skin")),
            second: plan("search", second, recommendation_requested=True),
        },
    )
    with TestClient(app) as client:
        app.state.product_search = Search()
        cid, other = new(client), new(client)
        commerce(client, other, "hello")
        previous = commerce(client, cid, first)
        commerce(client, cid, second)
        context = json.loads(calls[-1]["messages"][-1]["content"])
        assert [m["content"] for m in context["recent_dialogue"]] == [first, previous["reply"]]
        commerce(client, cid, "Start over")
        assert app.state.store.shopping_dialogue(cid) == []
        commerce(client, cid, first)
        assert "recent_dialogue" not in json.loads(calls[-1]["messages"][-1]["content"])
    with TestClient(create_app(config)) as restarted:
        history = restarted.app.state.store.shopping_dialogue(cid)
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[0]["content"] == first
        assert len(restarted.app.state.store.shopping_dialogue(other)) == 2


def test_history_cannot_restore_a_cleared_brand(tmp_path):
    first, clear, followup = "Acme moisturizer", "Any brand is fine", "Something else please"
    _, app, calls = harness(
        tmp_path,
        {
            first: plan("search", first, change("brand", "Acme", "Acme")),
            clear: plan("refine", clear, change("brand", None, clear)),
            followup: plan("refine", followup, change("brand", "Acme", "Acme")),
        },
    )
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        cid = new(client)
        commerce(client, cid, first)
        commerce(client, cid, clear)
        before = len(search.calls)
        result = commerce(client, cid, followup)
        assert result["validation_recovered"]
        assert result["preferences"]["tasks"][0]["filters"]["brand"] is None
        assert len(search.calls) == before
        assert len(calls) == 4  # two initial plans, then one repair of the rejected proposal


def test_dialogue_budget_reserves_state_and_repair_and_bounds_utf8():
    captured = []
    chat = ChatService(SimpleNamespace(), replace(Settings(), context_size=24000))

    async def fake(method, path, **kwargs):
        captured.append(kwargs["json"])
        return {
            "done": True,
            "message": {"content": json.dumps(plan("clarify", "help", scope="shopping"))},
        }

    chat.model_request = fake
    state = ShoppingState(tasks=[ShoppingTask(id=1, query="shampoo")], active_task_id=1)
    history = [
        {"role": role, "content": str(i) + "é" * 4000}
        for i in range(5)
        for role in ("user", "assistant")
    ]
    asyncio.run(chat.shopping_plan(state, "help", "invalid brand", dialogue=history))
    call = captured[-1]
    recent = json.loads(call["messages"][-1]["content"])["recent_dialogue"]
    assert len(recent) == 6 and recent[0]["content"].startswith("2")
    assert all(
        len(item["content"].encode("utf-8")) <= 1200 and item["truncated"] for item in recent
    )
    assert byte_size(call["messages"]) + byte_size(call["format"]) <= chat.settings.prompt_budget
    assert any("invalid brand" in item["content"] for item in call["messages"])

    # Set exactly enough room for the original state/schema/repair, without history or examples.
    original = call["messages"][:2]
    assert "Validation feedback" in original[1]["content"]
    context = json.loads(call["messages"][-1]["content"])
    context.pop("recent_dialogue")
    original.append(
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}
    )
    budget = byte_size(original) + byte_size(call["format"])
    chat.settings = replace(chat.settings, context_size=budget + chat.settings.max_tokens + 1024)
    asyncio.run(chat.shopping_plan(state, "help", "invalid brand", dialogue=history))
    small = captured[-1]
    assert "recent_dialogue" not in json.loads(small["messages"][-1]["content"])
    assert byte_size(small["messages"]) + byte_size(small["format"]) <= chat.settings.prompt_budget


@pytest.mark.parametrize(
    "need,budget,expected,absent",
    [
        ("brighten my face", "20", "skin type", "budget"),
        ("dry skin", None, "maximum budget", "skin type"),
        ("dry skin", "20", "browse skin-care", "budget"),
    ],
)
def test_clarification_asks_one_missing_question(need, budget, expected, absent):
    state = ShoppingState(
        tasks=[ShoppingTask(id=1, need=need, filters=ProductFilters(max_price=budget))],
        active_task_id=1,
    )
    search = Search()
    updated, result = handle_turn(
        state.model_dump(),
        IntentPlan(intent="clarify", evidence="not yet", recommendation_requested=False),
        "not yet",
        search.search,
    )
    assert result["reply"].count("?") == 1
    assert expected in result["reply"] and absent not in result["reply"]
    assert not search.calls and updated.active().filters == state.active().filters


def test_empty_results_question_uses_saved_constraints_without_relaxing_them():
    state = ShoppingState(
        tasks=[ShoppingTask(id=1, query="shampoo", filters=ProductFilters(brand="Missing"))],
        active_task_id=1,
    )
    search = Search()
    updated, result = handle_turn(
        state.model_dump(),
        IntentPlan(intent="search", evidence="show options"),
        "show options",
        search.search,
    )
    assert "brand?" in result["reply"] and "budget" not in result["reply"]
    assert updated.active().filters.brand == search.calls[-1].filters.brand == "Missing"


def test_compact_reply_preserves_conflicting_evidence_and_full_product_metadata():
    task = ShoppingTask(id=1, query="moisturizer", need="dry skin; sensitive skin")
    row = Search().search(ProductSearchRequest(query="moisturizer", limit=1))["results"][0]
    row["listing_evidence"].append({"text": "Not for sensitive skin.", "field": "/features/0"})
    products = explain_products([row], task)
    snapshot = json.dumps(products)
    compact, detailed = card_reply(products, compact=True), card_reply(products)
    assert "Listing warns against" in compact and "sensitive skin" in compact
    assert "Not for sensitive skin." in detailed
    assert len(compact) < len(detailed) and json.dumps(products) == snapshot


def test_summary_deduplicates_short_facts_and_preserves_prices_and_stream(monkeypatch):
    from app import commerce_summary

    async def fake(*args):
        yield '{"facts":["f1"]}', None

    monkeypatch.setattr(commerce_summary, "selection_stream", fake)
    task = ShoppingTask(id=1, query="moisturizer", need="dry skin")
    rows = Search().search(ProductSearchRequest(query="moisturizer", limit=1))["results"]
    products = explain_products(rows, task)
    value = {"products": products, "reply": card_reply(products, compact=True)}
    emitted = []
    asyncio.run(
        add_summary(
            SimpleNamespace(settings=Settings()),
            "dry skin",
            value,
            lambda _, data: emitted.append(data["text"]),
        )
    )
    assert value["reply"].count(concise_reasons(products[0])[0]) == 1
    assert "$5 USD" in value["reply"] and value["reply"].startswith("".join(emitted))
    assert value["summary"]["evidence"][0]["product_id"] == products[0]["product_id"]

"""Shopping continuations, semantic retrieval and recovery across all four sections."""

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_catalog import product
from test_commerce import change, commerce, harness, plan
from test_commerce_answer import APPROVED, fake_chat, sentence
from test_listing_enrichment import listing

from app.commerce import handle_turn
from app.commerce_answer import natural_answer
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.commerce_performance import response_route
from app.commerce_replies import explain_products
from app.commerce_retrieval import search_with_recovery
from app.commerce_storefront import handle_storefront_turn
from app.listing_explanations import concern_evidence
from app.listing_quality import listing_quality, prioritize_listing_evidence
from app.search_models import ProductFilters, ProductSearchRequest


class SectionSearch:
    def __init__(
        self, section="clothing", family="apparel", title="Comfortable soft fleece jacket"
    ):
        self.calls = []
        self.section, self.family, self.title = section, family, title

    def search(self, request):
        self.calls.append(request.model_copy(deep=True))
        rows = []
        for i in range(1, 21):
            row = product(f"B{i:09d}", "29.99")
            row["title"] = self.title + f" {i}"
            row["category"].update(
                domain="beauty" if self.section == "beauty" else "fashion", family=self.family
            )
            row["attributes"]["department"] = {"value": "mens", "field": "/title"}
            row["listing_evidence"] = [{"field": "/title", "text": row["title"]}]
            row["concern_evidence"] = concern_evidence(row, request.concerns)
            row["listing_quality"] = listing_quality(row, request, row["concern_evidence"])
            amount = Decimal(row["price"]["amount"])
            if request.filters.max_price is not None and amount > request.filters.max_price:
                continue
            if request.filters.min_price is not None and amount < request.filters.min_price:
                continue
            if row["product_id"] not in request.filters.exclude_product_ids:
                rows.append(row)
        return {
            "results": rows[: request.limit],
            "build_id": "fixture",
            "run_id": "fixture",
            "filters_applied": request.filters.model_dump(mode="json"),
        }


def scoped_turn(state, message, search, intent="refine", **kwargs):
    return handle_storefront_turn(
        state.model_dump(),
        IntentPlan(intent=intent, evidence=message, scope="shopping", **kwargs),
        message,
        search.search,
        handle_turn,
    )


def test_reported_jacket_chat_searches_after_each_preference_and_recovers_rejection(tmp_path):
    first, typo, correction, rejection = (
        "A jacket under $60",
        "need something comgy",
        "comfy and soft",
        "the list you showed are not comfy and soft jackets",
    )
    _, app, _ = harness(
        tmp_path,
        {
            first: plan(
                "search", first, change("query", "jacket", "jacket"), recommendation_requested=True
            ),
            typo: plan(
                "refine",
                typo,
                change("need", "comgy", "comgy"),
                recommendation_requested=False,
                interpretation={"query": "jacket comfortable", "evidence": "comgy"},
            ),
            correction: plan(
                "refine",
                correction,
                change("need", correction, correction),
                recommendation_requested=False,
                interpretation={"query": "jacket comfortable soft", "evidence": correction},
            ),
            rejection: plan(
                "feedback",
                rejection,
                feedback="dislike",
                recommendation_requested=False,
                interpretation={"query": "jacket soft comfortable fleece", "evidence": rejection},
            ),
        },
    )
    with TestClient(app) as client:
        app.state.product_search = search = SectionSearch()
        cid = client.post(
            "/conversations", json={"mode": "commerce", "storefront_id": "clothing"}
        ).json()["id"]
        for message in (first, "adult men", typo, correction):
            value = commerce(client, cid, message)
            assert value["action"] == "recommend" and value["products"]
            assert not value.get("validation_recovered")
        task = value["preferences"]["tasks"][0]
        assert task["need"] == correction
        assert task["sources"]["resolved_concerns"]["items"][0]["value"] == "comgy"
        before = {row["product_id"] for row in value["products"]}
        value = commerce(client, cid, rejection)
        assert value["action"] == "recommend" and value["products"]
        assert not before & {row["product_id"] for row in value["products"]}
        assert len(value["preferences"]["tasks"]) == 1
        assert all(r.filters.max_price == Decimal("59.99") for r in search.calls)
        assert all(r.filters.attributes == {"department": "mens"} for r in search.calls[1:])
        assert value["preferences"]["tasks"][0]["need"] == correction
        assert value["timings"]["search_calls"] >= 1


@pytest.mark.parametrize(
    "section,family,goal,meaning",
    [
        ("clothing", "apparel", "something comfy for office", "comfortable casual office clothing"),
        ("beauty", "skin_care", "help me find something for my dry skin", "skin care dry skin"),
        (
            "footwear",
            "footwear",
            "something for walking all day",
            "walking footwear cushioning comfort",
        ),
        (
            "fashion",
            "bags",
            "something understated for a wedding",
            "understated wedding accessories",
        ),
    ],
)
def test_goal_only_browsing_uses_the_section_without_inventing_a_subtype(
    section, family, goal, meaning
):
    search = SectionSearch(section, family)
    state, result = scoped_turn(
        ShoppingState(storefront_id=section),
        goal,
        search,
        intent="search",
        recommendation_requested=True,
        changes=[change("need", goal, goal)],
        interpretation={"query": meaning, "evidence": goal},
    )
    assert result["action"] == "recommend" and result["products"]
    assert search.calls[0].query == meaning
    assert search.calls[0].filters.storefront_id == section
    assert search.calls[0].filters.attributes == {}
    assert state.active().sources["query"]["specific_product_type_selected"] is False
    assert (
        state.active().sources["retrieval_interpretation"]["source"]
        == "semantic_retrieval_hypothesis"
    )


@pytest.mark.parametrize(
    "section,family,goal,preference,meaning",
    [
        ("clothing", "apparel", "jacket", "less formal", "casual jacket"),
        ("beauty", "hair_care", "shampoo", "gentler", "gentle shampoo"),
        ("footwear", "footwear", "shoes", "more comfy", "comfortable shoes"),
        ("fashion", "bags", "handbag", "understated", "minimalist handbag"),
    ],
)
def test_casual_preference_during_browsing_refreshes_all_categories(
    section, family, goal, preference, meaning
):
    search = SectionSearch(section, family)
    state, _ = scoped_turn(
        ShoppingState(storefront_id=section),
        goal,
        search,
        intent="search",
        recommendation_requested=True,
        changes=[change("query", goal, goal)],
    )
    before = len(search.calls)
    state, result = scoped_turn(
        state,
        preference,
        search,
        changes=[change("need", preference, preference)],
        recommendation_requested=False,
        interpretation={"query": meaning, "evidence": preference},
    )
    assert result["action"] == "recommend" and len(search.calls) > before
    assert state.active().need == preference
    assert search.calls[before].query == meaning
    assert search.calls[before].filters.attributes == {}


def test_pause_and_inspection_never_trigger_recommendations():
    search = SectionSearch()
    state, _ = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "jacket",
        search,
        intent="search",
        recommendation_requested=True,
    )
    before = len(search.calls)
    state, result = scoped_turn(
        state,
        "soft, don't show products yet",
        search,
        changes=[change("need", "soft", "soft")],
        recommendation_requested=True,
    )
    assert result["action"] == "clarify" and len(search.calls) == before
    state, result = scoped_turn(
        state,
        "what material is the first?",
        search,
        intent="answer",
        recommendation_requested=False,
    )
    assert result["action"] == "answer" and len(search.calls) == before


def test_semantic_interpretation_cannot_revive_removed_preferences_or_change_filters():
    search = SectionSearch()
    state = ShoppingState(
        storefront_id="clothing",
        active_task_id=1,
        tasks=[
            ShoppingTask(
                id=1,
                query="jacket",
                filters=ProductFilters(domain="fashion", family="apparel", max_price="40"),
            )
        ],
    )
    state, _ = scoped_turn(
        state,
        "soft",
        search,
        changes=[change("need", "soft", "soft")],
        recommendation_requested=True,
        interpretation={"query": "soft fleece jacket", "evidence": "soft"},
    )
    state, _ = scoped_turn(
        state,
        "remove the soft concern",
        search,
        changes=[change("need", None, "remove the soft concern")],
        recommendation_requested=True,
    )
    assert search.calls[-1].query == "jacket" and state.active().need == ""
    assert all(call.filters.max_price == 40 for call in search.calls)
    assert all(call.filters.attributes == {} for call in search.calls)


def test_recovery_is_bounded_and_preserves_all_constraints_and_exclusions():
    calls = []
    request = ProductSearchRequest(
        query="comfortable fleece jacket",
        product_goal="jacket",
        concerns=["soft"],
        filters=ProductFilters(
            storefront_id="clothing",
            max_price="40",
            brand="Acme",
            exclude_product_ids=["amazon:B000000001"],
            attributes={"department": "mens"},
        ),
    )

    def search(req):
        calls.append(req.model_copy(deep=True))
        return {"results": [] if len(calls) == 1 else [{"product_id": "amazon:B000000002"}]}

    result = search_with_recovery(search, request, "jacket soft")
    assert len(calls) == 2 and calls[0].filters == calls[1].filters == request.filters
    assert calls[0].product_goal == calls[1].product_goal == "jacket"
    assert result["retrieval_recovery"]["selected_attempt"] == 2
    assert result["retrieval_recovery"]["constraints_relaxed"] is False


def test_compound_preferences_require_both_and_show_real_listing_excerpts():
    full = listing("Comfortable fit.", "Soft fabric.")
    partial = listing("Comfortable fit.")
    negative = listing("Comfortable fit.", "Not soft fabric.")
    assert concern_evidence(full, ["comfy and soft"])[0]["support"] == "positive"
    assert concern_evidence(partial, ["comfy and soft"])[0]["support"] == "weak"
    assert concern_evidence(negative, ["comfy and soft"])[0]["support"] == "mixed"
    task = ShoppingTask(id=1, query="jacket", need="comfy and soft")
    row = explain_products([full], task)[0]
    assert "Comfortable fit." in row["why_recommended"][-1]


def test_unrequested_costumes_rank_below_everyday_jackets():
    request = ProductSearchRequest(query="soft jacket", product_goal="jacket", concerns=["soft"])
    rows = []
    for title in ("Soft medieval steampunk jacket costume", "Soft everyday jacket"):
        row = listing("Soft fabric.")
        row["title"], row["ranking"] = title, {}
        row["listing_quality"] = listing_quality(
            row, request, concern_evidence(row, request.concerns)
        )
        rows.append(row)
    result = prioritize_listing_evidence({"method": "test", "results": rows}, request)
    assert result["results"][0]["title"] == "Soft everyday jacket"
    request.product_goal = "medieval costume jacket"
    assert not listing_quality(rows[0], request, [])["cautions"]


def test_open_ended_results_use_reviewed_natural_writing_and_keep_the_products():
    search = SectionSearch()
    state, value = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "comfy jacket",
        search,
        intent="search",
        recommendation_requested=True,
        changes=[change("query", "jacket", "jacket"), change("need", "comfy", "comfy")],
        interpretation={"query": "comfortable jacket", "evidence": "comfy"},
    )
    value["intent"] = {"interpretation": {"query": "comfortable jacket"}}
    chat, _ = fake_chat(
        [sentence("Option 1 mentions comfort in its listing.", "p1"), APPROVED],
        commerce_natural=True,
    )
    assert response_route(chat.settings, "comfy jacket", value)["mode"] == "reviewed"
    products = deepcopy(value["products"])
    asyncio.run(natural_answer(chat, state, "comfy jacket", value))
    assert value["answer_generation"]["status"] == "generated"
    assert value["products"] == products and value["reply"].startswith("Option 1 mentions comfort")
    assert "historical USD" in value["reply"]


@pytest.mark.parametrize("reaction", ["meh", "too flashy", "not my vibe", "wrong direction"])
def test_bare_rejection_applies_to_the_latest_selection_without_another_question(reaction):
    search = SectionSearch()
    state, previous = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "jacket",
        search,
        intent="search",
        recommendation_requested=True,
    )
    ids = {row["product_id"] for row in previous["products"]}
    state, result = scoped_turn(
        state,
        reaction,
        search,
        intent="feedback",
        feedback="dislike",
        recommendation_requested=False,
    )
    assert result["action"] == "recommend" and result["products"]
    assert not ids & {row["product_id"] for row in result["products"]}
    assert set(state.active().feedback[-1].product_ids) == ids


def test_explicit_goal_in_a_section_overcomes_an_optional_subtype_question():
    search = SectionSearch("beauty", "hair_care", "Frizz hair care")
    message = "need something for frizzy hair"
    state, result = scoped_turn(
        ShoppingState(storefront_id="beauty"),
        message,
        search,
        intent="clarify",
        recommendation_requested=False,
        changes=[change("need", "frizzy hair", "frizzy hair")],
    )
    assert result["action"] == "recommend" and result["products"]
    assert state.active().query == "beauty and personal care"
    assert search.calls[0].filters.family is None


def test_same_vibe_but_cheaper_can_refine_the_whole_selection():
    search = SectionSearch()
    state, _ = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "jacket",
        search,
        intent="search",
        recommendation_requested=True,
    )
    _, result = scoped_turn(
        state,
        "same vibe but cheaper",
        search,
        intent="similar",
        cheaper_than_reference=True,
        recommendation_requested=True,
    )
    assert result["action"] == "recommend"
    assert search.calls[-1].filters.max_price == Decimal("29.98")
    assert search.calls[-1].product_goal == "jacket"


def test_next_page_keeps_the_semantic_goal_after_first_scoped_search():
    search = SectionSearch()
    state, _ = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "comfy jacket",
        search,
        intent="search",
        recommendation_requested=True,
        changes=[change("query", "jacket", "jacket"), change("need", "comfy", "comfy")],
        interpretation={"query": "comfortable everyday jacket", "evidence": "comfy"},
    )
    state, result = scoped_turn(state, "more", search, intent="more", recommendation_requested=True)
    assert result["products"] and search.calls[-1].query == "comfortable everyday jacket"
    assert len(search.calls[-1].filters.exclude_product_ids) == 5

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


def test_compound_goal_without_and_requires_evidence_for_occasion_and_comfort():
    full = listing("Comfortable fit for office workwear.")
    partial = listing("Comfortable lounge clothing.")
    concern = "Help me find something comfy for office"
    assert concern_evidence(full, [concern])[0]["support"] == "positive"
    evidence = concern_evidence(partial, [concern])[0]
    assert evidence["support"] == "weak"
    assert {item["concern"]: item["support"] for item in evidence["components"]} == {
        "comfy": "positive",
        "office": "unknown",
    }


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


def test_open_ended_results_can_use_grounded_reply_without_optional_writing():
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
    assert response_route(chat.settings, "comfy jacket", value)["mode"] == "fast"
    assert response_route(chat.settings, "explain why these fit", value)["mode"] == "reviewed"
    products = deepcopy(value["products"])
    asyncio.run(natural_answer(chat, state, "comfy jacket", value))
    assert value["answer_generation"]["status"] == "generated"
    assert value["products"] == products and value["reply"].startswith("Option 1 mentions comfort")
    assert "historical USD" in value["reply"]


def test_result_criticism_is_not_saved_as_a_positive_need_or_repeated():
    search = SectionSearch()
    state, previous = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "jacket under $60",
        search,
        intent="search",
        recommendation_requested=True,
    )
    state, result = scoped_turn(
        state,
        "these look too formal",
        search,
        changes=[change("need", "too formal", "too formal")],
        recommendation_requested=True,
        interpretation={"query": "casual jacket", "evidence": "too formal"},
    )
    assert state.active().need == ""
    assert state.active().feedback[-1].sentiment == "dislike"
    assert not {r["product_id"] for r in previous["products"]} & {
        r["product_id"] for r in result["products"]
    }
    assert search.calls[-1].query == "casual jacket"
    assert search.calls[-1].filters.max_price == Decimal("59.99")
    assert "left out the options you rejected" in result["reply"]


@pytest.mark.parametrize(
    "message", ["not too formal", "are these too formal?", "too formal but I like the second"]
)
def test_reaction_recovery_does_not_guess_questions_or_mixed_sentiment(message):
    from app.commerce_feedback import recover_reaction

    state = ShoppingState(tasks=[ShoppingTask(id=1, shown=[{"title": "Jacket"}])], active_task_id=1)
    intent = IntentPlan(intent="refine", evidence=message)
    recover_reaction(state, intent, message)
    assert intent.feedback is None


def test_feedback_examples_survive_pagination_but_stay_scoped_to_the_task():
    import json

    from app.commerce_prompts import planner_context

    search = SectionSearch()
    state, previous = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "jacket",
        search,
        intent="search",
        recommendation_requested=True,
    )
    liked = previous["products"][0]
    state, _ = scoped_turn(
        state,
        "I like the first",
        search,
        intent="feedback",
        feedback="like",
        recommendation_requested=False,
    )
    state, _ = scoped_turn(state, "more", search, intent="more", recommendation_requested=True)
    assert liked["product_id"] not in {row["product_id"] for row in state.active().shown}
    context = json.loads(planner_context(state, "more like the one I liked"))
    assert context["tasks"][0]["feedback_examples"][0]["title"] == liked["title"]
    state.tasks.append(ShoppingTask(id=2, query="shirt"))
    state.active_task_id = 2
    context = json.loads(planner_context(state, "more"))
    assert all("feedback_examples" not in task for task in context["tasks"])


def test_partial_match_notice_survives_one_confirmed_and_one_unknown_preference():
    from app.commerce_replies import recommendation_reply

    task = ShoppingTask(id=1, query="jacket", need="soft; breathable")
    products = [
        {
            "title": "Soft jacket",
            "price": {"amount": "20"},
            "concern_evidence": [
                {"concern": "soft", "support": "positive", "status": "listing_mention"},
                {"concern": "breathable", "support": "unknown", "status": "not_stated"},
            ],
        }
    ]
    result = recommendation_reply(
        task, products, IntentPlan(intent="refine", evidence="breathable")
    )
    assert "partial matches" in result


def test_budget_correction_is_described_as_current_budget_with_readable_recipient():
    from app.commerce_replies import recommendation_reply

    task = ShoppingTask(
        id=1,
        query="jacket",
        filters=ProductFilters(max_price="19.99", attributes={"department": "mens"}),
    )
    row = {
        "title": "Jacket",
        "price": {"amount": "18.99"},
        "why_recommended": ["Its listed historical price is within your $19.99 maximum."],
    }
    result = recommendation_reply(
        task, [row], IntentPlan(intent="refine", evidence="under $20 instead")
    )
    assert result.count("19.99") == 1
    assert "Budget: up to $19.99" in result and "For adult men" in result
    assert "Kept" not in result and "mens" not in result


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


def test_negative_ranking_terms_persist_on_more_and_explicit_positive_need_overrides_them():
    from app.commerce_browsing import retrieval_avoid_terms

    search = SectionSearch()
    state, _ = scoped_turn(
        ShoppingState(storefront_id="clothing"),
        "jacket",
        search,
        intent="search",
        recommendation_requested=True,
    )
    state, _ = scoped_turn(
        state,
        "too formal",
        search,
        intent="refine",
        recommendation_requested=True,
        interpretation={
            "query": "casual jacket",
            "evidence": "too formal",
            "avoid_terms": ["formal", "dressy"],
        },
    )
    assert search.calls[-1].avoid_terms == ["formal", "dressy"]
    state, _ = scoped_turn(state, "more", search, intent="more", recommendation_requested=True)
    assert search.calls[-1].avoid_terms == ["formal", "dressy"]
    state.active().need = "formal"
    assert "formal" not in retrieval_avoid_terms(state.active())
    assert all(not call.filters.attributes for call in search.calls)

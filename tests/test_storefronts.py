"""Storefront boundaries, state persistence and routing using deterministic model fixtures."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, change, commerce, harness, plan
from test_search_cache import MemoryClient

from app.catalog import CatalogError
from app.commerce import handle_turn, matching_families
from app.commerce_models import FAMILIES, IntentPlan, ShoppingState, ShoppingTask
from app.commerce_storefront import handle_storefront_turn, route_request
from app.main import Store
from app.search_cache import SearchCache
from app.search_filters import filter_sql
from app.search_models import ProductFilters, ProductSearchRequest
from app.storefronts import FAMILY_STOREFRONT, STOREFRONTS, category_allowed

PRODUCTS = {
    "skin_care": "moisturizer",
    "hair_care": "shampoo",
    "makeup": "lipstick",
    "fragrance": "perfume",
    "oral_care": "toothpaste",
    "shaving": "razor",
    "beauty_tools": "makeup brush",
    "personal_care": "deodorant",
    "apparel": "shirt",
    "footwear": "shoes",
    "bags": "handbag",
    "jewelry": "necklace",
    "watches": "watch",
    "accessories": "belt",
}


def test_taxonomy_is_complete_exclusive_and_preserves_domains():
    assert set(FAMILY_STOREFRONT) == set(FAMILIES) == set(PRODUCTS)
    assert sum(len(s["families"]) for s in STOREFRONTS.values()) == 14
    for family, (domain, _) in FAMILIES.items():
        assert category_allowed({"domain": domain, "family": family}, FAMILY_STOREFRONT[family])


@pytest.mark.parametrize("scope", STOREFRONTS)
@pytest.mark.parametrize("family,product", PRODUCTS.items())
@pytest.mark.parametrize(
    "template",
    [
        "I need {item}",
        "Please find {item} under $30",
        "Show me {item}",
        "Can you suggest {item} for me?",
    ],
)
def test_224_explicit_routing_cases(scope, family, product, template):
    message = template.format(item=product)
    state = ShoppingState(storefront_id=scope)
    proposal = IntentPlan(intent="search", evidence=message, recommendation_requested=True)
    routed = route_request(state, proposal, message)
    expected = FAMILY_STOREFRONT[family]
    if scope == expected:
        assert routed is None
    else:
        new_state, result = routed
        assert new_state == state and not result["products"]
        assert result["handoff"]["targets"][0]["storefront_id"] == expected
        assert result["handoff"]["targets"][0]["message"] == message


@pytest.mark.parametrize("scope", STOREFRONTS)
def test_scope_sql_is_an_intersection_not_a_replacement(scope):
    filters = ProductFilters(
        storefront_id=scope, family="skin_care", max_price="15", attributes={"color": "red"}
    )
    sql, values = filter_sql(filters)
    assert "p.family=ANY(%s)" in sql and "p.family=%s" in sql and "p.domain=%s" in sql
    assert list(STOREFRONTS[scope]["families"]) in values and Decimal("15") in values
    assert "skin_care" in values and "red" in values


def test_cache_keys_separate_all_sections_and_legacy():
    cache = SearchCache(client=MemoryClient())
    pinned = {"build_id": "fixture", "config": {"model_digest": "fixture"}}
    keys = {
        cache.key(
            ProductSearchRequest(query="something", filters=ProductFilters(storefront_id=scope)),
            pinned,
            {},
        )
        for scope in [None, *STOREFRONTS]
    }
    assert len(keys) == 5


def test_handoff_preserves_original_constraints_and_separates_multi_requests():
    state = ShoppingState(
        storefront_id="beauty",
        active_task_id=1,
        tasks=[
            ShoppingTask(
                id=1, query="shampoo", filters=ProductFilters(family="hair_care", max_price="12")
            )
        ],
    )
    original = state.model_dump()
    message = "shampoo under $15 and shirt under $40"
    routed = route_request(
        state, IntentPlan(intent="search", evidence=message, recommendation_requested=True), message
    )
    assert state.model_dump() == original == routed[0].model_dump()
    assert [(t["storefront_id"], t["message"]) for t in routed[1]["handoff"]["targets"]] == [
        ("beauty", "shampoo under $15"),
        ("clothing", "shirt under $40"),
    ]
    shared = "shampoo and shirt under $40 total"
    routed = route_request(state, IntentPlan(intent="search", evidence=shared), shared)
    assert routed[1]["action"] == "clarify" and routed[0].model_dump() == original


def test_negated_product_is_not_a_destination_and_ambiguous_product_is_not_forced():
    message = "no shampoo; I want a shirt"
    routed = route_request(
        ShoppingState(storefront_id="beauty"),
        IntentPlan(intent="search", evidence=message),
        message,
    )
    assert [t["storefront_id"] for t in routed[1]["handoff"]["targets"]] == ["clothing"]
    message = "something nice"
    assert (
        route_request(
            ShoppingState(storefront_id="beauty"),
            IntentPlan(intent="clarify", evidence=message, recommendation_requested=False),
            message,
        )
        is None
    )


def test_semantic_handoff_is_visible_and_does_not_change_preferences():
    message = "something for my sensitive scalp"
    state = ShoppingState(storefront_id="clothing")
    proposal = IntentPlan(
        intent="clarify",
        evidence=message,
        requested_storefront="beauty",
        recommendation_requested=False,
    )
    after, result = route_request(state, proposal, message)
    assert after == state and result["action"] == "handoff"
    assert result["handoff"]["targets"][0]["message"] == message


class ScopedSearch(Search):
    def search(self, request):
        result = super().search(request)
        for row in result["results"]:
            row["category"] = {"domain": request.filters.domain, "family": request.filters.family}
        return result


def test_normalization_changes_search_only_and_leaky_results_fail_closed():
    message = "shampoo for dandruf under $30"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            change("query", "shampoo", "shampoo"),
            change("need", "dandruf", "dandruf"),
        )
    )
    search = ScopedSearch()
    state, result = handle_storefront_turn(
        {"storefront_id": "beauty"}, proposal, message, search.search, handle_turn
    )
    assert state.active().need == "dandruf"
    assert "dandruff" in search.calls[0].query
    assert search.calls[0].filters.storefront_id == "beauty"
    assert result["search"]["original_query"] == "shampoo dandruf"
    assert state.active().filters.max_price == Decimal("29.99")

    def leaking(request):
        result = search.search(request)
        result["results"][0]["category"] = {"domain": "fashion", "family": "apparel"}
        return result

    with pytest.raises(CatalogError, match="different storefront"):
        handle_storefront_turn({"storefront_id": "beauty"}, proposal, message, leaking, handle_turn)


def test_real_http_state_isolation_restart_replay_and_legacy(tmp_path):
    message = "moisturizer under $30"
    switch = "a shirt under $40"
    config, app, _ = harness(
        tmp_path,
        {
            message: plan("search", message, change("query", "moisturizer", "moisturizer")),
            switch: plan("search", switch, change("query", "shirt", "shirt")),
        },
    )
    with TestClient(app) as client:
        app.state.product_search = ScopedSearch()
        ids = {
            scope: client.post(
                "/conversations", json={"mode": "commerce", "storefront_id": scope}
            ).json()["id"]
            for scope in STOREFRONTS
        }
        assert (
            client.post("/conversations", json={"storefront_id": "beauty"}).json()["mode"]
            == "commerce"
        )
        assert (
            client.post(
                "/conversations", json={"mode": "commerce", "storefront_id": "other"}
            ).status_code
            == 422
        )
        result = commerce(client, ids["beauty"], message)
        assert result["products"] and result["preferences"]["storefront_id"] == "beauty"
        before = app.state.store.get(ids["beauty"])["shopping_state"]
        endpoint = f"/conversations/{ids['beauty']}/messages"
        first = client.post(
            endpoint, json={"message": switch}, headers={"Idempotency-Key": "storefront-handoff-1"}
        )
        second = client.post(
            endpoint, json={"message": switch}, headers={"Idempotency-Key": "storefront-handoff-1"}
        )
        assert first.json() == second.json() and first.json()["commerce"]["action"] == "handoff"
        assert app.state.store.get(ids["beauty"])["shopping_state"] == before
        for scope in ["clothing", "footwear", "fashion"]:
            assert app.state.store.get(ids[scope])["shopping_state"] == {}
            assert client.get(f"/conversations/{ids[scope]}/messages").json()["messages"] == []
        assert len(app.state.product_search.calls) == 1
        legacy = client.post("/conversations", json={"mode": "commerce"}).json()
        assert legacy["storefront_id"] is None
    store = Store(config.database)
    assert store.get(ids["beauty"])["shopping_state"] == before
    assert store.get(ids["clothing"])["storefront_id"] == "clothing"


def test_corrupted_saved_products_cannot_leak_through_details():
    state = ShoppingState(
        storefront_id="beauty",
        active_task_id=1,
        tasks=[
            ShoppingTask(
                id=1,
                query="shampoo",
                shown=[{"category": {"domain": "fashion", "family": "apparel"}}],
            )
        ],
    )
    with pytest.raises(CatalogError, match="Saved shopping state"):
        handle_storefront_turn(
            state.model_dump(),
            IntentPlan(intent="details", evidence="details 1"),
            "details 1",
            Search().search,
            handle_turn,
        )


def test_postgres_both_retrieval_branches_hydration_and_empty_conflicts(
    pg, scratch_database, tmp_path
):
    from test_catalog import make_source, product
    from test_search import FakeEmbedder

    from app.catalog import connect_database, import_catalog, validated_source
    from app.product_search import search_catalog
    from app.search_index import build_index

    records = []
    for index, family in enumerate(PRODUCTS, 1):
        row = product(f"B{index:09d}")
        row["title"] = "Example " + PRODUCTS[family]
        row["category"].update(domain=FAMILIES[family][0], family=family)
        records.append(row)
    make_source(tmp_path, records)
    with validated_source(tmp_path) as source:
        import_catalog(pg, source)
    pg.commit()
    with connect_database(scratch_database, autocommit=True) as db:
        build_index(db, FakeEmbedder(), lambda *a, **kw: None)
    for scope, section in STOREFRONTS.items():
        request = ProductSearchRequest(
            query="Example", filters=ProductFilters(storefront_id=scope), limit=20, rerank=False
        )
        with connect_database(scratch_database) as db:
            result = search_catalog(db, request, FakeEmbedder())
        assert len(result["results"]) == len(section["families"])
        assert {row["category"]["family"] for row in result["results"]} == set(section["families"])
        assert all(
            row["ranking"]["keyword_rank"] and row["ranking"]["vector_rank"]
            for row in result["results"]
        )
        request.filters.family = "apparel" if scope == "beauty" else "skin_care"
        model = FakeEmbedder()
        with connect_database(scratch_database) as db:
            empty = search_catalog(db, request, model)
        assert not empty["results"] and empty["eligible_products"] == 0
        assert model.calls == 0


def test_evaluation_checks_do_not_confuse_isolation_with_usefulness():
    from app.storefront_evaluation import check_storefront_response

    empty = check_storefront_response(
        "beauty", {"commerce": {"action": "clarify", "products": []}}, []
    )
    assert empty["no_cross_storefront_products"] and empty["expected_destinations_match"]
    assert empty["human_relevance_review_required"]
    leaked = check_storefront_response(
        "beauty",
        {
            "commerce": {
                "action": "multi",
                "products": [],
                "groups": [
                    {"products": [{"category": {"family": "apparel", "domain": "fashion"}}]}
                ],
            }
        },
        [],
    )
    assert not leaked["no_cross_storefront_products"]
    failed = check_storefront_response("beauty", {"detail": "unavailable"}, ["clothing"])
    assert not failed["has_commerce_response"] and not failed["expected_destinations_match"]


def test_scoped_clarification_uses_only_its_section_examples():
    for scope, example in [
        ("clothing", "shirt"),
        ("beauty", "shampoo"),
        ("footwear", "sandals"),
        ("fashion", "watch"),
    ]:
        message = "something nice"
        _, response = handle_storefront_turn(
            {"storefront_id": scope},
            IntentPlan(intent="clarify", evidence=message, recommendation_requested=False),
            message,
            Search().search,
            handle_turn,
        )
        assert response["action"] == "clarify"
        assert example in response["reply"]
        assert not response["products"]


def test_scoped_planner_keeps_five_tasks_and_repair_within_context_budget(tmp_path):
    import json

    from app.text_budget import byte_size

    message = "shampoo under $10 instead"
    config, app, calls = harness(
        tmp_path,
        {
            message: [
                plan("refine", message, change("brand", "Invented", "shampoo")),
                plan("refine", message, change("max_price", "10", "$10")),
            ]
        },
    )
    with TestClient(app) as client:
        app.state.product_search = ScopedSearch()
        session = client.post(
            "/conversations", json={"mode": "commerce", "storefront_id": "beauty"}
        ).json()["id"]
        state = ShoppingState(
            storefront_id="beauty",
            active_task_id=1,
            tasks=[
                ShoppingTask(
                    id=i,
                    query=query,
                    need="a preference " * 15,
                    filters=ProductFilters(domain="beauty", family=matching_families(query)[0]),
                )
                for i, query in enumerate(
                    ["shampoo", "moisturizer", "cleanser", "lipstick", "perfume"], 1
                )
            ],
        )
        state.active().shown = [
            {
                "title": "Example listing " * 12,
                "brand": None,
                "category": {"domain": "beauty", "family": "hair_care"},
            }
            for _ in range(5)
        ]
        app.state.store.commit_commerce_turn(session, "initial", state, {"reply": "saved"}, 0)
        result = commerce(client, session, message)
        assert result["intent_attempts"] == 2
        for call in calls:
            assert byte_size(call["messages"]) + byte_size(call["format"]) <= config.prompt_budget
            context = json.loads(call["messages"][-1]["content"])
            assert len(context["tasks"]) == 5 and context["storefront_id"] == "beauty"


def test_fresh_preferences_expose_stored_scope_before_first_message(tmp_path):
    _, app, _ = harness(tmp_path, {})
    with TestClient(app) as client:
        record = client.post(
            "/conversations", json={"mode": "commerce", "storefront_id": "beauty"}
        ).json()
        prefs = client.get(f"/conversations/{record['id']}/preferences").json()["preferences"]
        assert prefs["storefront_id"] == "beauty"
        assert prefs["tasks"] == []
        assert app.state.store.get(record["id"])["shopping_state"] == {}


def test_error_responses_are_not_successful_empty_scope_checks():
    from app.storefront_evaluation import check_storefront_response

    for payload in [
        {"detail": "Unavailable"},
        {"commerce": None},
        {"commerce": {}},
        {"commerce": []},
    ]:
        result = check_storefront_response("beauty", payload, [])
        assert result["has_commerce_response"] is False
        assert result["no_cross_storefront_products"] is None
        assert result["expected_destinations_match"] is None

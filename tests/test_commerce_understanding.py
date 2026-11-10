"""Ordinary category language and scoped semantic goals keep literal evidence."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, change, commerce, harness, plan

from app.commerce import IntentError, handle_turn
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.commerce_storefront import handle_storefront_turn
from app.search_models import ProductFilters
from app.search_normalization import normalize_search


class ClothingSearch(Search):
    def search(self, request):
        self.calls.append(request.model_copy(deep=True))
        return {
            "results": [],
            "build_id": "understanding-fixture",
            "run_id": "understanding-fixture",
            "filters_applied": request.filters.model_dump(mode="json"),
        }


def proposal(message, query, *changes, section="clothing"):
    return IntentPlan.model_validate(
        plan(
            "search",
            message,
            change("query", query, query),
            *changes,
            scope="shopping",
            requested_storefront=section,
            recommendation_requested=True,
        )
    )


def test_reported_winter_clothing_requests_keep_male_and_reuse_one_task(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMERCE_NATURAL_REPLIES", "1")
    first, second = "need male clothes for winter", "winter cloths"
    _, app, calls = harness(
        tmp_path,
        {
            first: proposal(
                first,
                "clothes",
                change("recipient", "male", "male"),
                change("need", "winter", "winter"),
            ).model_dump(),
            second: proposal(second, second).model_dump(),
        },
    )
    with TestClient(app) as client:
        app.state.product_search = search = ClothingSearch()
        cid = client.post(
            "/conversations", json={"mode": "commerce", "storefront_id": "clothing"}
        ).json()["id"]
        for message in (first, second):
            result = commerce(client, cid, message)
            assert result["action"] == "recommend" and not result.get("validation_recovered")
            assert len(result["preferences"]["tasks"]) == 1
            task = result["preferences"]["tasks"][0]
            assert task["filters"]["family"] == "apparel"
            assert task["filters"]["attributes"] == {"department": "mens"}
            assert "winter" in task["need"]
            assert result["intent_attempts"] == 1
            assert result["answer_generation"]["status"] == "skipped"
        assert len(calls) == len(search.calls) == 2
        assert "clothing" in search.calls[-1].query
        assert "cloths" not in search.calls[-1].query
        assert task["query"] == second
        assert task["recipient"] == "male"


@pytest.mark.parametrize(
    "query", ["clothes", "clothing", "garments", "outfits", "outerwear", "winter cloths"]
)
def test_broad_clothing_does_not_require_a_specific_garment(query):
    message = "need male " + query + " for winter"
    search = ClothingSearch()
    state, result = handle_storefront_turn(
        {"storefront_id": "clothing"}, proposal(message, query), message, search.search, handle_turn
    )
    assert result["action"] == "recommend"
    assert state.active().query == query
    assert state.active().need == "winter"
    assert search.calls[-1].filters.attributes == {"department": "mens"}


@pytest.mark.parametrize(
    "query,section,family",
    [
        ("parka", "clothing", "apparel"),
        ("espadrilles", "footwear", "footwear"),
        ("balm", "beauty", "skin_care"),
        ("brooch", "fashion", "jewelry"),
    ],
)
def test_unfamiliar_literal_product_can_use_a_scoped_semantic_category(query, section, family):
    message = "I need a " + query
    search = ClothingSearch()
    extracted = proposal(message, query, change("family", family, query), section=section)
    state, _ = handle_storefront_turn(
        {"storefront_id": section}, extracted, message, search.search, handle_turn
    )
    assert state.active().query == query
    assert state.active().filters.family == family
    assert state.active().sources["family"]["source"] == "semantic_product_classification"
    assert state.active().sources["query"]["evidence"] == query


@pytest.mark.parametrize("query", ["winter", "something nice", "adult male"])
def test_a_context_only_phrase_cannot_become_an_invented_product(query):
    message = "I need " + query
    with pytest.raises(IntentError):
        handle_turn({}, proposal(message, query), message, ClothingSearch().search)


def test_unknown_product_classification_cannot_override_a_known_category():
    message = "I need shampoo"
    extracted = proposal(message, "shampoo", change("family", "apparel", "shampoo"))
    state, result = handle_storefront_turn(
        {"storefront_id": "clothing"}, extracted, message, ClothingSearch().search, handle_turn
    )
    assert not state.tasks and result["action"] == "handoff"
    assert result["handoff"]["targets"][0]["storefront_id"] == "beauty"


def test_alias_rewrite_preserves_literal_wording_and_existing_budget():
    saved = ShoppingState(
        storefront_id="clothing",
        active_task_id=1,
        tasks=[
            ShoppingTask(
                id=1,
                query="clothes",
                filters=ProductFilters(
                    family="apparel",
                    storefront_id="clothing",
                    max_price="35",
                    attributes={"department": "mens"},
                ),
            )
        ],
    )
    message = "winter cloths"
    extracted = proposal(message, message)
    extracted.requested_storefront = None
    extracted.changes[0].value = "winter clothing"
    state, result = handle_storefront_turn(
        saved.model_dump(), extracted, message, ClothingSearch().search, handle_turn
    )
    assert result["action"] == "recommend" and len(state.tasks) == 1
    assert state.active().filters.max_price == Decimal("35")
    assert state.active().query == message and "winter" in state.active().need


def test_typo_alias_is_scoped_and_does_not_rewrite_cleaning_cloths():
    assert normalize_search("winter cloths", storefront_id="clothing") == "winter clothing"
    assert normalize_search("winter cloths") == "winter cloths"
    assert (
        normalize_search("microfiber cleaning cloths", storefront_id="clothing")
        == "microfiber cleaning cloths"
    )


def test_season_recovery_does_not_split_hair_fall_into_an_autumn_preference():
    message = "shampoo for hair fall"
    state, _ = handle_turn(
        {},
        proposal(message, "shampoo", change("need", "hair fall", "hair fall"), section="beauty"),
        message,
        ClothingSearch().search,
    )
    assert [need.value for need in state.active().needs] == ["hair fall"]

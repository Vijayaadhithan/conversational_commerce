"""Regression conversations: literal preferences, product scope, replay, and recovery."""

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, change, commerce, harness, new, plan

from app.commerce import IntentError, handle_turn, matching_families
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.main import create_app
from app.product_search import ProductFilters


@pytest.mark.parametrize(
    "extracted",
    [
        change("need", "less chemical", "kess chemincal"),
        change("attribute.fragrance_free", True, "kess chemincal"),
        change("query", "gentle moisturizer", "kess chemincal"),
    ],
)
def test_reported_typo_followup_preserves_context_without_inventing_claims(tmp_path, extracted):
    first = "i need a prduct that lightens my face"
    followup = "something that is kess chemincal"
    plans = {
        first: plan("clarify", first, change("need", "lightens my face", "lightens my face")),
        followup: plan("refine", followup, extracted),
        "show options": plan("search", "show options"),
    }
    config, app, _ = harness(tmp_path, plans)
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        commerce(client, session, first)
        result = commerce(client, session, followup)
        assert result["action"] == "clarify" and "ingredient" in result["reply"]
        task = result["preferences"]["tasks"][0]
        assert "lightens my face" in task["need"] and "kess chemincal" in task["need"]
        assert "less chemical" not in task["need"] and task["filters"]["attributes"] == {}
        assert not search.calls
        result = commerce(client, session, "show options")
        assert result["products"] and search.calls[-1].filters.family == "skin_care"
        assert "kess chemincal" in search.calls[-1].query
        expected = client.get(f"/conversations/{session}/preferences").json()
    with TestClient(create_app(config)) as restarted:
        assert restarted.get(f"/conversations/{session}/preferences").json() == expected


def test_model_paraphrase_stores_literal_preference_not_its_interpretation():
    message = "my skin feels tight after washing"
    proposal = IntentPlan.model_validate(
        plan("refine", message, change("need", "dry skin", message))
    )
    state, _ = handle_turn({}, proposal, message, Search().search)
    assert state.active().need == message
    assert state.active().needs[0].evidence == message


def test_repeated_wrapped_concern_is_not_duplicated():
    message = "i need a prduct that lightens my face"
    state = ShoppingState(tasks=[ShoppingTask(id=1, need="lightens my face")], active_task_id=1)
    state, _ = handle_turn(
        state.model_dump(),
        IntentPlan.model_validate(plan("refine", message, change("need", message, message))),
        message,
        Search().search,
    )
    assert len(state.active().needs) == 1


@pytest.mark.parametrize("first,second", [("shampoo", "moisturizer"), ("cleanser", "moisturizer")])
def test_product_requests_keep_budgets_and_references_separate(tmp_path, first, second):
    message = f"{first} under $15 and {second} under $25"
    followup = f"{first} under $10 instead"
    plans = {
        message: plan(
            "search",
            message,
            requests=[
                {"evidence": f"{first} under $15", "changes": [change("query", first, first)]},
                {"evidence": f"{second} under $25", "changes": [change("query", second, second)]},
            ],
        ),
        followup: plan("refine", followup, change("max_price", "10", "$10")),
        "details about the first in task 2": plan("details", "details about the first in task 2"),
    }
    config, app, _ = harness(tmp_path, plans)
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        result = commerce(client, session, message)
        assert result["action"] == "multi" and len(result["groups"]) == 2
        assert [call.filters.max_price for call in search.calls] == [
            Decimal("14.99"),
            Decimal("24.99"),
        ]
        assert first not in search.calls[1].query and second not in search.calls[0].query
        tasks = result["preferences"]["tasks"]
        assert [task["query"] for task in tasks] == [first, second]
        assert all(
            row["task_id"] == group["task_id"]
            for group in result["groups"]
            for row in group["products"]
        )
        assert result["preferences"]["turns"] == 1
        ambiguous = commerce(client, session, "Compare the first two")
        assert ambiguous["action"] == "clarify" and len(search.calls) == 2
        assert ambiguous["preferences"]["tasks"] == tasks
        result = commerce(client, session, "details about the first in task 2")
        assert result["action"] == "details" and len(result["products"]) == 1
        result = commerce(client, session, followup)
        assert result["preferences"]["active_task_id"] == 1
        assert result["preferences"]["tasks"][1]["filters"]["max_price"] == "24.99"
        assert search.calls[-1].filters.max_price == Decimal("9.99")
        assert search.calls[-1].query.startswith(first)
        expected = client.get(f"/conversations/{session}/preferences").json()
    with TestClient(create_app(config)) as restarted:
        assert restarted.get(f"/conversations/{session}/preferences").json() == expected
        history = restarted.get(f"/conversations/{session}/messages").json()["messages"]
        assert len(history[1]["commerce"]["groups"]) == 2


def test_multi_followup_updates_named_tasks_without_creating_duplicates():
    search = Search()
    message = "shampoo under $15 and moisturizer under $25"
    state, _ = handle_turn(
        {}, IntentPlan(intent="search", evidence=message), message, search.search
    )
    message = "shampoo under $10 and moisturizer under $20"
    state, _ = handle_turn(
        state.model_dump(), IntentPlan(intent="refine", evidence=message), message, search.search
    )
    assert len(state.tasks) == 2
    assert [task.filters.max_price for task in state.tasks] == [Decimal("9.99"), Decimal("19.99")]
    assert all(not task.need for task in state.tasks)


@pytest.mark.parametrize(
    "message",
    ["shampoo and moisturizer under $30 total", "shampoo and cleanser both fragrance-free"],
)
def test_shared_requirement_scope_asks_before_searching(message):
    search = Search()
    state, result = handle_turn(
        {}, IntentPlan(intent="clarify", evidence=message), message, search.search
    )
    assert result["action"] == "clarify" and not state.tasks and not search.calls


def test_cross_product_evidence_cannot_leak_into_another_task():
    message = "shampoo under $15 and moisturizer under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {"evidence": "shampoo under $15", "changes": [change("max_price", "25", "$25")]},
                {"evidence": "moisturizer under $25", "changes": []},
            ],
        )
    )
    search = Search()
    with pytest.raises(IntentError):
        handle_turn({}, proposal, message, search.search)
    assert not search.calls


def test_fashion_clarification_does_not_block_ready_beauty_request():
    message = "a shirt and shampoo under $15"
    state, result = handle_turn(
        {}, IntentPlan(intent="search", evidence=message), message, Search().search
    )
    assert [group["action"] for group in result["groups"]] == ["clarify", "recommend"]
    assert state.tasks[0].pending_kind == "recipient"
    message = "the shirt is for men"
    state, result = handle_turn(
        state.model_dump(),
        IntentPlan.model_validate(
            plan("refine", message, change("attribute.department", "mens", "men"))
        ),
        message,
        Search().search,
    )
    assert state.active_task_id == 1 and state.active().filters.attributes == {"department": "mens"}
    assert state.tasks[1].filters.max_price == Decimal("14.99")


def test_new_product_same_family_starts_with_fresh_constraints():
    state = ShoppingState(
        tasks=[
            ShoppingTask(
                id=1,
                query="moisturizer",
                filters=ProductFilters(domain="beauty", family="skin_care", max_price="20"),
            )
        ],
        active_task_id=1,
    )
    message = "Now I need a cleanser"
    state, _ = handle_turn(
        state.model_dump(),
        IntentPlan.model_validate(plan("refine", message, change("query", "cleanser", "cleanser"))),
        message,
        Search().search,
    )
    assert len(state.tasks) == 2 and state.active().query == "cleanser"
    assert state.active().filters.max_price is None


def test_limit_is_atomic_and_does_not_silently_drop_a_product():
    state = ShoppingState(
        tasks=[ShoppingTask(id=i, query="lipstick") for i in range(1, 5)], active_task_id=4
    )
    message = "shampoo and moisturizer"
    search = Search()
    updated, result = handle_turn(
        state.model_dump(), IntentPlan(intent="search", evidence=message), message, search.search
    )
    assert result["action"] == "clarify" and len(updated.tasks) == 4 and not search.calls


def test_longest_product_name_wins_and_substrings_do_not_match():
    assert matching_families("makeup brush") == ["beauty_tools"]
    assert matching_families("shirt for spring") == ["apparel"]
    assert matching_families("watching my budget") == []


@pytest.mark.parametrize(
    "message",
    [
        "shampoo, conditioner, and moisturizer",
        "shampoo. Also moisturizer",
        "shampoo as well as moisturizer",
    ],
)
def test_common_list_and_sentence_forms_preserve_all_products(message):
    state, result = handle_turn(
        {}, IntentPlan(intent="search", evidence=message), message, Search().search
    )
    assert result["action"] == "multi"
    assert len(state.tasks) == (3 if "conditioner" in message else 2)


def test_optional_examples_make_room_for_saved_tasks_and_repair_feedback(tmp_path):
    from app.main import byte_size

    message = "shampoo under $10 instead"
    answers = [
        plan("refine", message, change("brand", "Invented", "shampoo")),
        plan("refine", message, change("max_price", "10", "$10")),
    ]
    config, app, calls = harness(tmp_path, {message: answers})
    with TestClient(app) as client:
        app.state.product_search = Search()
        session = new(client)
        tasks = [
            ShoppingTask(id=i, query=query, need="a preference " * 15)
            for i, query in enumerate(["shampoo", "moisturizer", "cleanser", "bag", "lipstick"], 1)
        ]
        state = ShoppingState(tasks=tasks, active_task_id=1)
        state.active().shown = [{"title": "Example listing " * 12, "brand": None} for _ in range(5)]
        app.state.store.commit_commerce_turn(session, "initial", state, {"reply": "saved"}, 0)
        result = commerce(client, session, message)
        assert result["intent_attempts"] == 2 and len(result["preferences"]["tasks"]) == 5
        for call in calls:
            assert byte_size(call["messages"]) + byte_size(call["format"]) <= config.prompt_budget
            assert json.loads(call["messages"][-1]["content"])["tasks"][-1]["id"] == 5


def test_each_product_enforces_its_own_attributes():
    message = "fragrance-free shampoo under $15 and Acme moisturizer under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {
                    "evidence": "fragrance-free shampoo under $15",
                    "changes": [change("attribute.fragrance_free", True, "fragrance-free")],
                },
                {
                    "evidence": "Acme moisturizer under $25",
                    "changes": [change("brand", "Acme", "Acme")],
                },
            ],
        )
    )
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert result["action"] == "multi"
    assert search.calls[0].filters.attributes == {"fragrance_free": True}
    assert search.calls[0].filters.brand is None
    assert search.calls[1].filters.brand == "Acme" and search.calls[1].filters.attributes == {}


def test_failed_second_search_does_not_commit_first_product(tmp_path):
    from test_commerce import send

    from app.catalog import CatalogError

    message = "shampoo and moisturizer"
    _, app, _ = harness(tmp_path, {message: plan("search", message)})
    with TestClient(app) as client:
        search = Search()
        original = search.search

        def fail_second(request):
            if search.calls:
                raise CatalogError("Injected second search failure")
            return original(request)

        search.search = fail_second
        app.state.product_search = search
        session = new(client)
        assert send(client, session, message).status_code == 503
        assert (
            client.get(f"/conversations/{session}/preferences").json()["preferences"]["tasks"] == []
        )
        assert client.get(f"/conversations/{session}/messages").json()["messages"] == []


@pytest.mark.parametrize(
    "message",
    [
        "give me soem products with the above conditions",
        "what would you pick for me?",
        "anything matching what we discussed?",
        "okay go ahead",
        "I would like a few choices based on that",
        "give me products that are less chemical",
    ],
)
def test_semantic_browse_decision_uses_saved_concerns_without_magic_words(tmp_path, message):
    _, app, calls = harness(
        tmp_path, {message: plan("refine", message, recommendation_requested=True)}
    )
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        state = ShoppingState(
            tasks=[
                ShoppingTask(
                    id=1,
                    need="lightens my face; kess chemincal",
                    filters=ProductFilters(max_price="20", brand="Acme"),
                )
            ],
            active_task_id=1,
            pending_kind="preference_detail",
            pending_question="Which ingredient do you mean? You can browse related listings.",
        )
        app.state.store.commit_commerce_turn(
            session, "something that is kess chemincal", state, {"reply": state.pending_question}, 0
        )
        result = commerce(client, session, message)
        assert result["action"] == "recommend" and result["products"]
        assert len(calls) == 1 and len(search.calls) == 1
        request = search.calls[0]
        assert "lightens my face" in request.query and "kess chemincal" in request.query
        assert request.filters.family == "skin_care" and request.filters.attributes == {}
        assert request.filters.max_price == Decimal("20") and request.filters.brand == "Acme"
        assert "recommendation_requested" in calls[0]["format"]["required"]
        assert (
            result["preferences"]["tasks"][0]["sources"]["query"]["source"]
            == "concern_guided_discovery"
        )


@pytest.mark.parametrize("query", ["", "moisturizer"])
@pytest.mark.parametrize(
    "message",
    [
        "Don't suggest products yet",
        "What does 'show options' do?",
        "Before you recommend anything, what does fragrance-free mean?",
        "What does under $20 mean?",
    ],
)
def test_semantic_refusal_or_discussion_does_not_trigger_keyword_retrieval(query, message):
    state = ShoppingState(
        tasks=[ShoppingTask(id=1, query=query, need="dry skin")],
        active_task_id=1,
        pending_kind="skin_profile",
    )
    search = Search()
    proposal = IntentPlan(intent="search", evidence=message, recommendation_requested=False)
    updated, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "clarify" and not search.calls
    assert updated.active().query == query and updated.active().need == "dry skin"
    assert updated.active().filters == state.active().filters


def test_browse_decision_does_not_waive_unsupported_requirements():
    state = ShoppingState(
        tasks=[ShoppingTask(id=1, need="dry skin", unsupported=["delivery by tomorrow"])],
        active_task_id=1,
    )
    message = "give me something matching all that"
    search = Search()
    updated, result = handle_turn(
        state.model_dump(),
        IntentPlan(intent="search", evidence=message, recommendation_requested=True),
        message,
        search.search,
    )
    assert (
        result["action"] == "clarify" and updated.pending_kind == "unsupported" and not search.calls
    )
    assert updated.active().unsupported == ["delivery by tomorrow"]


@pytest.mark.parametrize(
    "message,expected_tasks",
    [("Now I also need a shirt", 2), ("I do not need a shirt", 1)],
)
def test_new_task_goal_recovery_is_separate_from_recommendation_intent(message, expected_tasks):
    state = ShoppingState(
        tasks=[
            ShoppingTask(
                id=1,
                query="moisturizer",
                need="I have dry skin",
                filters=ProductFilters(domain="beauty", family="skin_care", max_price="19.99"),
            )
        ],
        active_task_id=1,
    )
    search = Search()
    proposal = IntentPlan(intent="new_task", evidence=message, recommendation_requested=False)
    updated, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "clarify" and not search.calls
    assert len(updated.tasks) == expected_tasks
    pending = {"pending_question", "pending_kind"}
    assert updated.tasks[0].model_dump(exclude=pending) == state.tasks[0].model_dump(
        exclude=pending
    )
    if expected_tasks == 2:
        assert updated.active().query == "shirt"
        assert updated.active().filters.family == "apparel"
        assert updated.active().filters.max_price is None
        assert updated.active().need == ""


def test_browse_decision_still_needs_enough_context_to_choose_a_category():
    message = "give me some options"
    search = Search()
    updated, result = handle_turn(
        {},
        IntentPlan(intent="search", evidence=message, recommendation_requested=True),
        message,
        search.search,
    )
    assert result["action"] == "clarify" and updated.active().query == "" and not search.calls


def test_concerns_wrapped_as_product_requests_still_use_one_task_and_validate_fields():
    state = ShoppingState(
        tasks=[
            ShoppingTask(
                id=1,
                query="skin care",
                need="lightens my face; kess chemincal",
                filters=ProductFilters(domain="beauty", family="skin_care"),
            )
        ],
        active_task_id=1,
    )
    message = "that lighten my face, reduce pimble, and so on"
    proposal = IntentPlan.model_validate(
        plan(
            "refine",
            message,
            recommendation_requested=False,
            requests=[
                {
                    "evidence": "lighten my face",
                    "changes": [change("query", "serum", "lighten my face")],
                },
                {
                    "evidence": "reduce pimble",
                    "changes": [change("need", "reduce pimble", "reduce pimble")],
                },
            ],
        )
    )
    search = Search()
    updated, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "clarify" and not search.calls and len(updated.tasks) == 1
    assert "reduce pimble" in updated.active().need and updated.active().query == "skin care"
    invalid = IntentPlan.model_validate(
        plan(
            "refine",
            message,
            requests=[
                {
                    "evidence": "reduce pimble",
                    "changes": [change("brand", "Invented", "reduce pimble")],
                }
            ],
        )
    )
    with pytest.raises(IntentError):
        handle_turn(state.model_dump(), invalid, message, search.search)


@pytest.mark.parametrize("amount", ["$20", "20 USD", "USD 20", "20 dollars", "20$"])
def test_browsing_accepts_quoted_currency_format_without_relaxing_budget(amount):
    message = "oily skin, under $20, show me options"
    state = ShoppingState(
        tasks=[ShoppingTask(id=1, need="lightens my face; kess chemincal")], active_task_id=1
    )
    proposal = IntentPlan.model_validate(
        plan(
            "refine",
            message,
            change("max_price", amount, "under $20"),
            recommendation_requested=True,
        )
    )
    search = Search()
    updated, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "recommend" and search.calls[-1].filters.max_price == Decimal(
        "19.99"
    )
    assert "lightens my face" in updated.active().need
    invalid = IntentPlan.model_validate(
        plan(
            "refine",
            message,
            change("max_price", "$200", "under $20"),
            recommendation_requested=True,
        )
    )
    with pytest.raises(IntentError):
        handle_turn(state.model_dump(), invalid, message, search.search)


def test_extracted_amounts_do_not_leave_budget_words_as_concerns():
    message = "shampoo under $15 and moisturizer under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {"evidence": "shampoo under $15", "changes": [change("max_price", "$15", "$15")]},
                {
                    "evidence": "moisturizer under $25",
                    "changes": [change("max_price", "$25", "$25")],
                },
            ],
        )
    )
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi" and all(not task.need for task in state.tasks)
    assert [task.filters.max_price for task in state.tasks] == [Decimal("14.99"), Decimal("24.99")]


@pytest.mark.parametrize("product", ["lipstick", "razor blades", "handbag"])
def test_single_request_wrapper_is_validated_as_one_task(product):
    message = f"Recommend {product} under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            recommendation_requested=True,
            requests=[{"evidence": message, "changes": [change("query", product, product)]}],
        )
    )
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert len(state.tasks) == 1 and state.active().query == product
    assert search.calls[-1].filters.max_price == Decimal("24.99")


@pytest.mark.parametrize("concern_value", ["dandruf", "dandruff"])
@pytest.mark.parametrize("amount", ["30", "30$"])
def test_shampoo_with_wrapped_concerns_is_one_request(tmp_path, concern_value, amount):
    message = "hair fall need a shampoo and dandruf under 30$"
    proposal = plan(
        "search",
        message,
        recommendation_requested=True,
        requests=[
            {
                "evidence": "hair fall need a shampoo",
                "changes": [
                    change("query", "shampoo", "shampoo"),
                    change("need", "hair fall", "hair fall"),
                ],
            },
            {
                "evidence": "dandruf under 30$",
                "changes": [
                    change("need", concern_value, "dandruf"),
                    change("max_price", amount, "under 30$"),
                ],
            },
        ],
    )
    _, app, calls = harness(tmp_path, {message: proposal})
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        result = commerce(client, new(client), message)
        assert result["action"] == "recommend"
        assert len(calls) == len(search.calls) == 1
        tasks = result["preferences"]["tasks"]
        assert len(tasks) == 1 and tasks[0]["query"] == "shampoo"
        assert {item["value"] for item in tasks[0]["needs"]} == {"hair fall", "dandruf"}
        request = search.calls[0]
        assert request.filters.family == "hair_care"
        assert request.filters.max_price == Decimal("29.99")
        assert request.filters.attributes == {}
        assert "hair fall" in request.query and "dandruf" in request.query


@pytest.mark.parametrize(
    "extra",
    [
        {"evidence": "dandruff", "changes": []},
        {"evidence": "dandruf", "changes": [change("max_price", "30", "under 30$")]},
        {"evidence": "dandruf", "changes": [change("brand", "Invented", "dandruf")]},
    ],
)
def test_single_product_wrapper_recovery_keeps_evidence_validation(extra):
    message = "hair fall need a shampoo and dandruf under 30$"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            recommendation_requested=True,
            requests=[
                {"evidence": "shampoo", "changes": [change("query", "shampoo", "shampoo")]},
                extra,
            ],
        )
    )
    search = Search()
    with pytest.raises(IntentError):
        handle_turn({}, proposal, message, search.search)
    assert not search.calls


def test_multi_clauses_accept_omitted_request_verb_but_keep_complete_requirements():
    message = "Recommend shampoo under $15 and moisturizer under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            recommendation_requested=True,
            requests=[
                {"evidence": "shampoo under $15", "changes": []},
                {"evidence": "moisturizer under $25", "changes": []},
            ],
        )
    )
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi" and not any(t.need for t in state.tasks)
    assert [t.filters.max_price for t in state.tasks] == [Decimal("14.99"), Decimal("24.99")]
    message = "Back to shampoo"
    state, _ = handle_turn(
        state.model_dump(),
        IntentPlan(intent="switch_task", evidence=message, task_id=1),
        message,
        Search().search,
    )
    message = "Under $10 instead"
    state, _ = handle_turn(
        state.model_dump(), IntentPlan(intent="refine", evidence=message), message, Search().search
    )
    assert state.active_task_id == 1
    assert [t.filters.max_price for t in state.tasks] == [Decimal("9.99"), Decimal("24.99")]


@pytest.mark.parametrize("recipient,department", [("adult women", "womens"), ("adult men", "mens")])
def test_explicit_recipient_omission_recovered_without_reasking(recipient, department):
    message = f"Recommend shoes for {recipient} under $75"
    proposal = IntentPlan(intent="search", evidence=message, recommendation_requested=True)
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert result["action"] == "recommend"
    assert state.active().recipient == recipient
    assert search.calls[-1].filters.attributes["department"] == department


def test_no_match_retains_impossible_budget_and_named_product():
    message = "Recommend a moisturizer under $0.01"
    state, result = handle_turn(
        {},
        IntentPlan(intent="clarify", evidence=message, recommendation_requested=True),
        message,
        Search().search,
    )
    assert result["action"] == "recommend" and not result["products"]
    assert "no moisturizer listings" in result["reply"]
    assert state.active().filters.max_price == Decimal("0.00")


def test_allergy_question_explains_limits_without_changing_saved_constraints():
    saved = ShoppingState(
        tasks=[ShoppingTask(id=1, query="moisturizer", filters=ProductFilters(max_price="20"))],
        active_task_id=1,
    )
    message = "Would these be guaranteed safe for my allergies?"
    state, result = handle_turn(
        saved.model_dump(), IntentPlan(intent="clarify", evidence=message), message, Search().search
    )
    assert "cannot guarantee allergy safety" in result["reply"]
    assert state.tasks == saved.tasks and state.pending_kind == "evidence_limit"


def test_facial_product_goal_is_kept_separately_from_soft_concerns():
    message = "Recommend a face moisturizer for dry skin"
    proposal = IntentPlan.model_validate(
        plan("search", message, change("query", "moisturizer", "moisturizer"))
    )
    search = Search()
    state, _ = handle_turn({}, proposal, message, search.search)
    assert search.calls[-1].product_goal == "face moisturizer"
    assert "dry skin" in search.calls[-1].concerns


@pytest.mark.parametrize("domain,family", [("beauty", "apparel"), ("apparel", "mens")])
def test_product_mapping_repairs_model_taxonomy_codes(domain, family):
    message = "Recommend a cotton shirt with pockets for adult men under $30"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            change("query", "cotton shirt", "cotton shirt"),
            change("need", "with pockets", "with pockets"),
            change("domain", domain, "shirt"),
            change("family", family, "men"),
        )
    )
    search = Search()
    state, _ = handle_turn({}, proposal, message, search.search)
    assert state.active().filters.domain == "fashion" and state.active().filters.family == "apparel"
    assert search.calls[-1].product_goal == "cotton shirt with pockets"


def test_return_to_short_product_name_preserves_specific_goal_and_budget():
    saved = ShoppingState(
        tasks=[
            ShoppingTask(id=1, query="red lipstick", filters=ProductFilters(max_price="18")),
            ShoppingTask(id=2, query="shampoo", filters=ProductFilters(max_price="10")),
        ],
        active_task_id=2,
    )
    message = "Back to lipstick"
    proposal = IntentPlan.model_validate(
        plan("switch_task", message, change("query", "lipstick", "lipstick"), task_id=1)
    )
    state, result = handle_turn(saved.model_dump(), proposal, message, Search().search)
    assert state.active_task_id == 1 and len(state.tasks) == 2
    assert state.active().query == "red lipstick" and state.active().filters.max_price == Decimal(
        "18"
    )


def test_compound_bag_name_does_not_hide_a_second_product_clause():
    message = "crossbody handbag under $30 and shampoo under $12"
    state, result = handle_turn(
        {}, IntentPlan(intent="search", evidence=message), message, Search().search
    )
    assert len(state.tasks) == 2 and result["action"] == "multi"
    assert [t.filters.max_price for t in state.tasks] == [Decimal("29.99"), Decimal("11.99")]


def test_explicit_definition_resolves_vague_preference_with_audit_evidence():
    from app.commerce_models import Concern

    saved = ShoppingState(
        tasks=[
            ShoppingTask(
                id=1,
                query="skin care",
                need="brighten my face; less chemical",
                needs=[
                    Concern(value="brighten my face", evidence="brighten my face"),
                    Concern(value="less chemical", evidence="less chemical"),
                ],
            )
        ],
        active_task_id=1,
        pending_kind="preference_detail",
    )
    message = "I mean fragrance-free, show me products"
    proposal = IntentPlan(intent="search", evidence=message, recommendation_requested=True)
    state, _ = handle_turn(saved.model_dump(), proposal, message, Search().search)
    assert state.active().need == "brighten my face"
    assert state.active().filters.attributes["fragrance_free"] is True
    assert state.active().sources["resolved_preference"]["evidence"] == message


def test_model_comparator_arithmetic_is_accepted_only_when_code_reproduces_it():
    message = "moisturizer under $10"
    proposal = IntentPlan.model_validate(
        plan("search", message, change("max_price", "9.99", "under $10"))
    )
    state, _ = handle_turn({}, proposal, message, Search().search)
    assert state.active().filters.max_price == Decimal("9.99")
    proposal = IntentPlan.model_validate(
        plan("search", message, change("max_price", "8.99", "under $10"))
    )
    with pytest.raises(IntentError):
        handle_turn({}, proposal, message, Search().search)


def test_empty_search_labels_previous_products_before_comparison():
    message = "moisturizer under $20"
    state, _ = handle_turn(
        {}, IntentPlan(intent="search", evidence=message), message, Search().search
    )
    assert state.active().shown
    message = "Under $0.01 instead"
    state, _ = handle_turn(
        state.model_dump(), IntentPlan(intent="refine", evidence=message), message, Search().search
    )
    assert state.active().shown
    message = "Compare the first two"
    state, result = handle_turn(
        state.model_dump(), IntentPlan(intent="compare", evidence=message), message, Search().search
    )
    assert (
        result["action"] == "compare"
        and "not confirmed to match your current requirements" in result["reply"]
    )


@pytest.mark.parametrize(
    "message",
    ["Make the budget $20", "Set it to twenty dollars", "Please change my budget to 20 USD"],
)
def test_explicit_budget_commands_refresh_results_after_no_match(message):
    from app.commerce import direct_plan

    state = ShoppingState(
        tasks=[ShoppingTask(id=1, query="moisturizer", filters=ProductFilters(max_price="0"))],
        active_task_id=1,
    )
    proposal = direct_plan(message)
    assert proposal and proposal.recommendation_requested
    state, result = handle_turn(state.model_dump(), proposal, message, Search().search)
    assert result["products"] and state.active().filters.max_price == Decimal("20")


@pytest.mark.parametrize(
    "message",
    ["What does set my budget to $20 mean?", "Set my budget to $20 but don't show products yet"],
)
def test_budget_discussion_or_deferral_still_goes_through_semantic_planner(message):
    from app.commerce import direct_plan

    assert direct_plan(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "More expensive products",
        "No more choices",
        "Show me more details",
        "More options but don't show products yet",
    ],
)
def test_more_options_router_does_not_capture_other_requests(message):
    from app.commerce import direct_plan

    assert direct_plan(message) is None


def test_frontend_task_actions_preserve_scope_and_skip_seen_products():
    from app.commerce import direct_plan

    search = Search()
    message = "moisturizer under $20"
    state, first = handle_turn(
        {}, IntentPlan(intent="search", evidence=message), message, search.search
    )
    state.tasks.append(ShoppingTask(id=2, query="shampoo"))
    state.active_task_id = 2
    state.displayed_task_ids = [1, 2]
    message = "More options for task 1"
    proposal = direct_plan(message)
    assert proposal.intent == "more" and proposal.task_id == 1
    state, more = handle_turn(state.model_dump(), proposal, message, search.search)
    assert more["products"] and state.active_task_id == 1
    assert not {p["product_id"] for p in first["products"]} & {
        p["product_id"] for p in more["products"]
    }
    assert state.tasks[1].filters.max_price is None
    message = "Compare the first two in task 1"
    state, compared = handle_turn(state.model_dump(), direct_plan(message), message, search.search)
    assert compared["action"] == "compare" and len(compared["products"]) == 2
    product_id = compared["products"][0]["product_id"]
    message = f"Details about {product_id} in task 1"
    state, detail = handle_turn(state.model_dump(), direct_plan(message), message, search.search)
    assert detail["action"] == "details" and detail["products"][0]["product_id"] == product_id
    message = "Cheaper options for task 1"
    state, _ = handle_turn(state.model_dump(), direct_plan(message), message, search.search)
    assert state.active().filters.max_price < Decimal(more["products"][0]["price"]["amount"])


@pytest.mark.parametrize(
    "message",
    [
        "More expensive products for task 1",
        "No more choices for task 1",
        "More options but don't show products yet for task 1",
        "More options for task 1 and task 2",
        "More options for task 1 for task 2",
    ],
)
def test_task_suffix_does_not_swallow_freeform_or_negated_requests(message):
    from app.commerce import direct_plan

    assert direct_plan(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "shampoo and hair conditioner for dandruff",
        "i need shampoo and hair conditioner for dandruff",
        "shampoo and hair conditioner for dandruf",
        "shampoo and hair conditioner for dry hair",
    ],
)
@pytest.mark.parametrize("shape", ["expanded", "literal", "top_level", "flat"])
def test_coordinated_products_keep_the_same_literal_concern(message, shape):
    need = message.split(" for ", 1)[1]
    first = message.split(" and ", 1)[0]
    requests = [
        {
            "evidence": "shampoo for " + need if shape == "expanded" else first,
            "changes": [change("query", "shampoo", "shampoo")],
        },
        {
            "evidence": "hair conditioner for " + need,
            "changes": [change("query", "hair conditioner", "hair conditioner")],
        },
    ]
    changes = []
    if shape == "expanded":
        for request in requests:
            request["changes"].append(change("need", need, need))
    elif shape == "literal":
        requests[1]["changes"].append(change("need", need, need))
    else:
        changes = [change("need", need, need)]
    if shape == "flat":
        changes += [change("query", "shampoo and hair conditioner", "shampoo and hair conditioner")]
        requests = []
    search = Search()
    proposal = IntentPlan.model_validate(plan("search", message, *changes, requests=requests))
    state, result = handle_turn({}, proposal, message, search.search)
    assert result["action"] == "multi" and len(state.tasks) == 2
    assert len(search.calls) == 2
    for task, request in zip(state.tasks, search.calls):
        assert task.need == need and need in request.query
        assert task.filters.max_price is None and task.filters.attributes == {}
        assert all(item.evidence in message for item in task.needs)
    assert "conditioner" not in search.calls[0].query
    assert "shampoo" not in search.calls[1].query


def test_shared_concern_is_not_limited_to_a_named_hair_condition():
    message = "dress and shoes for a wedding"
    proposal = IntentPlan.model_validate(
        plan("search", message, change("need", "a wedding", "a wedding"))
    )
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi"
    assert [task.need for task in state.tasks] == ["a wedding", "a wedding"]


@pytest.mark.parametrize(
    "message",
    [
        "shampoo for dandruff and conditioner for dry hair",
        "shampoo. Hair conditioner for dry hair",
        "shampoo; hair conditioner for dry hair",
        "red shampoo and hair conditioner for dry hair",
    ],
)
def test_shared_concern_does_not_cross_independent_product_clauses(message):
    # A model must not transfer the last product's concern to an earlier qualified clause.
    from app.commerce_requests import multi_requests

    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {
                    "evidence": message.split(" and ")[0].split(".")[0].split(";")[0],
                    "changes": [change("need", "dry hair", "dry hair")],
                },
                {"evidence": "conditioner for dry hair", "changes": []},
            ],
        )
    )
    with pytest.raises(IntentError):
        multi_requests(proposal, message)


def test_shared_concern_does_not_legalize_manufactured_preference_quotes():
    message = "shampoo and conditioner for dandruff"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {
                    "evidence": "shampoo for dandruff",
                    "changes": [
                        change("query", "shampoo", "shampoo for dandruff"),
                        change("need", "dandruff", "dandruff"),
                    ],
                },
                {"evidence": "conditioner for dandruff", "changes": []},
            ],
        )
    )
    with pytest.raises(IntentError):
        handle_turn({}, proposal, message, Search().search)


def test_shared_concern_preserves_existing_budgets_and_scoped_followups(tmp_path):
    message = "shampoo and hair conditioner for dandruff"
    initial = plan(
        "search",
        message,
        requests=[
            {
                "evidence": "shampoo for dandruff",
                "changes": [
                    change("query", "shampoo", "shampoo"),
                    change("need", "dandruff", "dandruff"),
                ],
            },
            {
                "evidence": "hair conditioner for dandruff",
                "changes": [
                    change("query", "hair conditioner", "hair conditioner"),
                    change("need", "dandruff", "dandruff"),
                ],
            },
        ],
    )
    config, app, _ = harness(
        tmp_path,
        {
            message: initial,
            "shampoo under $10": plan(
                "refine", "shampoo under $10", change("max_price", "10", "$10")
            ),
        },
    )
    with TestClient(app) as client:
        app.state.product_search = Search()
        session = client.post(
            "/conversations", json={"mode": "commerce", "storefront_id": "beauty"}
        ).json()["id"]
        saved = ShoppingState(
            storefront_id="beauty",
            active_task_id=1,
            tasks=[
                ShoppingTask(
                    id=1,
                    query="shampoo",
                    filters=ProductFilters(domain="beauty", family="hair_care", max_price=15),
                ),
                ShoppingTask(
                    id=2,
                    query="hair conditioner",
                    filters=ProductFilters(domain="beauty", family="hair_care", max_price=25),
                ),
            ],
        )
        app.state.store.commit_commerce_turn(session, "initial", saved, {"reply": "saved"}, 0)
        result = commerce(client, session, message)
        tasks = result["preferences"]["tasks"]
        assert result["action"] == "multi" and not result.get("validation_recovered")
        assert len(tasks) == 2 and [t["need"] for t in tasks] == ["dandruff", "dandruff"]
        assert [t["filters"]["max_price"] for t in tasks] == ["15", "25"]
        result = commerce(client, session, "shampoo under $10")
        assert result["preferences"]["tasks"][0]["filters"]["max_price"] == "9.99"
        assert result["preferences"]["tasks"][1]["filters"]["max_price"] == "25"
        history = client.get(f"/conversations/{session}/messages").json()["messages"]
        assert history[-3]["commerce"]["action"] == "multi"
    with TestClient(create_app(config)) as client:
        assert client.get(f"/conversations/{session}/messages").json()["messages"] == history


def test_shared_compound_concern_is_saved_once_per_product():
    message = "shampoo and conditioner for dry hair and frizz"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {
                    "evidence": "shampoo for dry hair and frizz",
                    "changes": [
                        change("query", "shampoo", "shampoo"),
                        change("need", "dry hair", "dry hair"),
                        change("need", "frizz", "frizz"),
                    ],
                },
                {
                    "evidence": "conditioner for dry hair and frizz",
                    "changes": [
                        change("query", "conditioner", "conditioner"),
                        change("need", "dry hair", "dry hair"),
                        change("need", "frizz", "frizz"),
                    ],
                },
            ],
        )
    )
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi"
    assert [task.need for task in state.tasks] == ["dry hair and frizz"] * 2
    assert all(len(task.needs) == 1 for task in state.tasks)


def test_mixed_top_level_and_nested_product_plan_preserves_typo_and_all_products():
    message = "shampoo and conditioner for dandruf"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            change("query", "shampoo", "shampoo"),
            change("need", "dandruf", "dandruf"),
            requests=[
                {
                    "evidence": "conditioner for dandruf",
                    "changes": [
                        change("query", "conditioner", "conditioner"),
                        change("need", "dandruf", "dandruf"),
                    ],
                }
            ],
        )
    )
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi"
    assert [task.query for task in state.tasks] == ["shampoo", "conditioner"]
    assert [task.need for task in state.tasks] == ["dandruf", "dandruf"]


def test_reordered_product_plan_uses_evidence_not_array_position():
    message = "shampoo under $15 and conditioner under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {
                    "evidence": "conditioner under $25",
                    "changes": [change("max_price", "25", "$25")],
                },
                {"evidence": "shampoo under $15", "changes": [change("max_price", "15", "$15")]},
            ],
        )
    )
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi"
    assert [task.query for task in state.tasks] == ["shampoo", "conditioner"]
    assert [str(task.filters.max_price) for task in state.tasks] == ["14.99", "24.99"]


def test_duplicate_product_proposals_fail_without_searching():
    message = "shampoo under $15 and conditioner under $25"
    proposal = IntentPlan.model_validate(
        plan(
            "search",
            message,
            requests=[
                {"evidence": "shampoo", "changes": []},
                {"evidence": "shampoo", "changes": []},
            ],
        )
    )
    search = Search()
    with pytest.raises(IntentError):
        handle_turn({}, proposal, message, search.search)
    assert not search.calls

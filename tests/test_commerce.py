"""State transitions, evidence boundaries, replay, and failure atomicity without model weights."""

from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from app.catalog import CatalogError
from app.commerce import handle_turn, number_text
from app.commerce_models import IntentPlan, ShoppingState, ShoppingTask
from app.main import Settings, Store, create_app
from app.product_search import ProductFilters
from test_catalog import product


def change(field, value, evidence):
    return dict(field=field, value=value, evidence=evidence)


def plan(intent, message, *changes, **extra):
    return dict(intent=intent, evidence=message[:500], changes=list(changes), **extra)


class Search:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.empty = False

    def search(self, request):
        self.calls.append(request.model_copy(deep=True))
        if self.fail: raise CatalogError("Injected search failure")
        rows = []
        for i in range(1, 31):
            row = product(f"B{i:09d}", str(Decimal(4 + i)))
            row["title"] = f"Moisturizer for dry skin {i}"
            row["brand"] = {"value": "Acme" if i % 2 else "Other", "field": "/details/Brand", "status": "listing_claim"}
            row["listing_evidence"] = [{"field": "/title", "text": row["title"], "status": "listing_excerpt"}]
            filters = request.filters
            if row["product_id"] in filters.exclude_product_ids: continue
            if row["brand"]["value"] in filters.exclude_brands: continue
            if filters.brand and filters.brand != row["brand"]["value"]: continue
            if filters.max_price is not None and Decimal(row["price"]["amount"]) > filters.max_price: continue
            if filters.min_price is not None and Decimal(row["price"]["amount"]) < filters.min_price: continue
            if filters.attributes or self.empty: continue
            rows.append(row)
        return dict(results=rows[:request.limit], build_id="fixture-build", run_id="fixture-run",
                    filters_applied=request.filters.model_dump(mode="json"), price_context="historical USD")


def harness(tmp_path, plans):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["format"]["title"] == "IntentPlan"
        assert body["options"]["temperature"] == 0
        context = json.loads(body["messages"][-1]["content"])
        answer = plans[context["latest_message"]]
        if isinstance(answer, list): answer = answer.pop(0)
        if isinstance(answer, Exception): raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": content}})

    config = replace(Settings(), database=str(tmp_path / "commerce.sqlite3"))
    app = create_app(config, httpx.MockTransport(handler))
    return config, app, calls


def new(client):
    result = client.post("/conversations", json={"mode": "commerce"})
    assert result.status_code == 201
    return result.json()["id"]


def send(client, session, message):
    return client.post(f"/conversations/{session}/messages", json={"message": message})


def commerce(client, session, message):
    result = send(client, session, message)
    assert result.status_code == 200, result.text
    return result.json()["commerce"]


def test_clarify_then_recommend_refine_compare_more_and_restart(tmp_path):
    plans = {
        "I have dry skin": plan("clarify", "I have dry skin", change("need", "dry skin", "dry skin")),
        "A moisturizer under $30": plan("search", "A moisturizer under $30", change("query", "moisturizer", "moisturizer"), change("max_price", "30", "under $30")),
        "Under $20 instead": plan("refine", "Under $20 instead", change("max_price", "20", "Under $20")),
        "Use Acme": plan("refine", "Use Acme", change("brand", "Acme", "Acme")),
        "Any brand is fine": plan("refine", "Any brand is fine", change("brand", None, "Any brand is fine")),
    }
    config, app, calls = harness(tmp_path, plans)
    search = Search()
    with TestClient(app) as client:
        app.state.product_search = search
        session, other = new(client), new(client)
        assert commerce(client, session, "I have dry skin")["action"] == "clarify"
        assert not search.calls
        result = commerce(client, session, "A moisturizer under $30")
        assert result["products"] and result["products"][0]["why_recommended"]
        assert search.calls[-1].query == "moisturizer dry skin"
        assert search.calls[-1].filters.max_price == Decimal("29.99")
        result = commerce(client, session, "Under $20 instead")
        assert search.calls[-1].query == "moisturizer dry skin"
        assert search.calls[-1].filters.max_price == Decimal("19.99")
        assert result["preferences"]["tasks"][0]["sources"]["max_price"]["source"] == "user_numeric_comparator"
        before = len(calls), len(search.calls)
        compared = commerce(client, session, "Compare the first two")
        assert compared["action"] == "compare" and "$1.00 USD" in compared["reply"]
        details = commerce(client, session, "Details about the second")
        assert details["products"][0]["product_id"] == result["products"][1]["product_id"]
        assert details["reply"].startswith("2.")
        assert before == (len(calls), len(search.calls))
        seen = {row["product_id"] for row in result["products"]}
        more = commerce(client, session, "More options")
        assert seen.isdisjoint(row["product_id"] for row in more["products"])
        assert set(search.calls[-1].filters.exclude_product_ids) == seen
        assert search.calls[-1].filters.max_price == Decimal("19.99")
        commerce(client, session, "Use Acme")
        assert search.calls[-1].filters.brand == "Acme"
        commerce(client, session, "Any brand is fine")
        assert search.calls[-1].filters.brand is None
        assert client.get(f"/conversations/{other}/preferences").json()["preferences"]["tasks"] == []
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 16
    with TestClient(create_app(config)) as client:
        state = client.get(f"/conversations/{session}/preferences").json()
        assert state["preferences"]["tasks"][0]["need"] == "dry skin"
        assert state["preferences"]["tasks"][0]["filters"]["max_price"] == "19.99"
        assert state["state_version"] == 8
        replay = client.get(f"/conversations/{session}/messages").json()["messages"]
        assert replay[3]["commerce"]["products"][0]["product_id"] == "amazon:B000000001"
        assert client.delete(f"/conversations/{session}").status_code == 204
        with app.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM commerce_turns").fetchone()[0] == 0


@pytest.mark.parametrize("bad_change", [
    change("max_price", None, "moisturizer"),
    change("max_price", "10", "moisturizer"),
    change("brand", "Invented", "moisturizer"),
    change("family", True, "moisturizer"),
    change("domain", True, "moisturizer"),
    change("need", "eczema", "moisturizer"),
    change("attribute.fragrance_free", True, "unscented"),
    change("attribute.fragrance_free", False, "unscented"),
    change("query", "moisturizer for acne", "moisturizer"),
])
def test_untrusted_extraction_cannot_invent_or_remove_preferences(tmp_path, bad_change):
    message = "moisturizer unscented"
    _, app, _ = harness(tmp_path, {message: plan("search", message, bad_change)})
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        result = commerce(client, session, message)
        assert result["action"] == "clarify" and result["validation_recovered"]
        assert not search.calls
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 2
        saved = client.get(f"/conversations/{session}/preferences").json()
        assert saved["state_version"] == 1 and saved["preferences"]["tasks"] == []


@pytest.mark.parametrize("output", ['not json', '{"intent":"search","intent":"reset","evidence":"moisturizer"}', '{"intent":"search","evidence":"moisturizer","unknown":true}'])
def test_malformed_plan_is_not_committed(tmp_path, output):
    _, app, _ = harness(tmp_path, {"moisturizer": output})
    with TestClient(app) as client:
        session = new(client)
        assert send(client, session, "moisturizer").status_code == 502
        assert client.get(f"/conversations/{session}/preferences").json()["state_version"] == 0


def test_empty_results_keep_filters_and_previous_reference_snapshot(tmp_path):
    message = "moisturizer"
    stricter = "Make it fragrance-free"
    plans = {message: plan("search", message, change("query", message, message)),
             stricter: plan("refine", stricter, change("attribute.fragrance_free", True, "fragrance-free"))}
    _, app, _ = harness(tmp_path, plans)
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        original = commerce(client, session, message)
        empty = commerce(client, session, stricter)
        assert empty["products"] == [] and "unchanged" in empty["reply"]
        assert search.calls[-1].filters.attributes["fragrance_free"] is True
        details = commerce(client, session, "Details about the first")
        assert details["products"][0] == original["products"][0]
        assert details["preferences"]["tasks"][0]["filters"]["attributes"]["fragrance_free"] is True


def test_failed_search_rolls_back_state_and_messages(tmp_path):
    message = "moisturizer under $20"
    _, app, _ = harness(tmp_path, {message: plan("search", message, change("query", "moisturizer", "moisturizer"))})
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        search.fail = True
        session = new(client)
        assert send(client, session, message).status_code == 503
        assert client.get(f"/conversations/{session}/preferences").json()["preferences"]["tasks"] == []
        assert client.get(f"/conversations/{session}/messages").json()["messages"] == []
        search.fail = False
        assert commerce(client, session, message)["products"]


def test_separate_tasks_restore_requirements_and_ask_recipient(tmp_path):
    plans = {
        "moisturizer under $20": plan("search", "moisturizer under $20", change("query", "moisturizer", "moisturizer")),
        "Now a shirt": plan("new_task", "Now a shirt", change("query", "shirt", "shirt")),
        "For men": plan("refine", "For men", change("recipient", "men", "men"), change("attribute.department", "mens", "men")),
        "Back to moisturizer": plan("switch_task", "Back to moisturizer", task_id=1),
    }
    _, app, _ = harness(tmp_path, plans)
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        commerce(client, session, "moisturizer under $20")
        response = commerce(client, session, "Now a shirt")
        assert response["action"] == "clarify" and "Who are these for" in response["reply"]
        assert response["preferences"]["tasks"][1]["filters"]["max_price"] is None
        commerce(client, session, "For men")
        assert search.calls[-1].filters.attributes == {"department": "mens"}
        assert search.calls[-1].filters.domain == "fashion"
        restored = commerce(client, session, "Back to moisturizer")
        assert restored["preferences"]["active_task_id"] == 1
        assert search.calls[-1].filters.attributes == {}
        assert search.calls[-1].filters.max_price == Decimal("19.99")


def test_unsupported_requirement_needs_explicit_relaxation(tmp_path):
    message, yes = "moisturizer in stock under 500 INR", "yes"
    _, app, _ = harness(tmp_path, {message: plan("search", message, change("query", "moisturizer", "moisturizer"), change("max_price", "500", "under 500 INR")), yes: plan("refine", yes)})
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        blocked = commerce(client, session, message)
        assert blocked["action"] == "clarify" and "cannot verify" in blocked["reply"]
        assert not search.calls
        assert commerce(client, session, yes)["products"]
        # The INR amount was never treated as a USD maximum.
        assert search.calls[-1].filters.max_price is None


def test_cheaper_other_brand_reset_and_budget_conflict(tmp_path):
    message = "moisturizer between $5 and $20"
    _, app, _ = harness(tmp_path, {message: plan("search", message, change("query", "moisturizer", "moisturizer")),
        "Under $3 instead": plan("refine", "Under $3 instead", change("max_price", "3", "Under $3"))})
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        commerce(client, session, message)
        cheaper = commerce(client, session, "Cheaper")
        assert cheaper["action"] == "clarify" and "conflict" in cheaper["reply"]
        conflict = commerce(client, session, "Under $3 instead")
        assert conflict["action"] == "clarify"
        assert conflict["preferences"]["tasks"][0]["filters"]["max_price"] == "20"
        commerce(client, session, "Another brand")
        assert set(search.calls[-1].filters.exclude_brands) == {"Acme", "Other"}
        assert commerce(client, session, "Start over")["preferences"]["tasks"] == []
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 10


def test_migration_stale_write_and_long_term_state(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',summarized_through INTEGER NOT NULL DEFAULT 0)""")
    store = Store(path)
    session = store.create("Existing", "commerce")["id"]
    task = ShoppingTask(id=1, query="moisturizer", need="dry skin", filters=ProductFilters(max_price="20"))
    state = ShoppingState(tasks=[task], active_task_id=1)
    metadata = {"reply": "saved", "products": []}
    for version in range(30):
        store.commit_commerce_turn(session, "hello", state, metadata, version)
    assert store.get(session)["shopping_state"]["tasks"][0]["need"] == "dry skin"
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        store.commit_commerce_turn(session, "stale", state, metadata, 29)
    assert error.value.status_code == 409
    assert len(store.messages(session)) == 60
    assert Store(path).get(session)["state_version"] == 30


def test_word_numbers_and_negative_amounts():
    assert number_text("under twenty-five dollars") == "under 25 dollars"
    assert number_text("under -20 dollars") == "under -20 dollars"
    message = "moisturizer under twenty dollars"
    saved, _ = handle_turn({}, IntentPlan.model_validate(plan("search", message, change("query", "moisturizer", "moisturizer"))), message, Search().search)
    assert saved.active().filters.max_price == Decimal("19.99")


def test_full_history_does_not_drop_exclusion_ids():
    task = ShoppingTask(id=1, query="moisturizer", seen_ids=[f"amazon:B{i:09d}" for i in range(100)])
    state = ShoppingState(tasks=[task], active_task_id=1)
    search = Search()
    state, response = handle_turn(state.model_dump(), IntentPlan(intent="more", evidence="more"), "more", search.search)
    assert response["action"] == "clarify" and not search.calls
    assert len(state.active().seen_ids) == 100


def test_initial_unknown_goal_does_not_erase_a_stated_concern():
    message = "I have dry skin"
    proposal = IntentPlan.model_validate(plan("new_task", message, change("need", "dry skin", "dry skin"), change("query", None, message)))
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert result["action"] == "clarify" and state.active().need == "dry skin"
    assert not state.active().query and not search.calls


def test_explicit_goal_completes_pending_task_despite_wrong_action_label():
    state = ShoppingState(tasks=[ShoppingTask(id=1, need="dry skin")], active_task_id=1)
    message = "Show me a moisturizer under $30"
    proposal = IntentPlan.model_validate(plan("switch_task", message, change("query", "moisturizer", "moisturizer")))
    search = Search()
    state, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "recommend" and state.active().need == "dry skin"
    assert len(state.tasks) == 1 and search.calls[-1].filters.max_price == Decimal("29.99")


@pytest.mark.parametrize("message,field,value,evidence", [
    ("Not fragrance-free", "attribute.fragrance_free", True, "fragrance-free"),
    ("Avoid Acme", "brand", "Acme", "Acme"),
    ("Not red", "attribute.color", "red", "red"),
    ("Don't remove my budget", "max_price", None, "Don't remove my budget"),
    ("US size 8", "attribute.size", "8", "8"),
])
def test_negation_and_sizing_system_cannot_be_lost(tmp_path, message, field, value, evidence):
    _, app, _ = harness(tmp_path, {message: plan("refine", message, change(field, value, evidence))})
    with TestClient(app) as client:
        session = new(client)
        state = ShoppingState(tasks=[ShoppingTask(id=1, query="moisturizer", filters=ProductFilters(max_price="20", brand="Acme",
            attributes={"fragrance_free": True, "color": "red", "size": "8"}))], active_task_id=1)
        app.state.store.commit_commerce_turn(session, "initial", state, {"reply": "saved"}, 0)
        result = commerce(client, session, message)
        assert result["action"] == "clarify" and result["validation_recovered"]
        saved = client.get(f"/conversations/{session}/preferences").json()
        assert saved["state_version"] == 2
        assert saved["preferences"]["tasks"][0]["filters"] == state.active().filters.model_dump(mode="json")


def test_partial_reset_instruction_cannot_erase_all_requirements(tmp_path):
    message = "Forget my budget"
    _, app, _ = harness(tmp_path, {message: plan("reset", message)})
    with TestClient(app) as client:
        session = new(client)
        assert commerce(client, session, message)["validation_recovered"]


def test_inherited_restatement_keeps_original_source_and_is_not_a_new_change():
    original = {"value": "dry skin", "evidence": "I have dry skin", "source": "user"}
    state = ShoppingState(tasks=[ShoppingTask(id=1, need="dry skin", sources={"need": original})], active_task_id=1)
    message = "Show me a moisturizer"
    proposal = IntentPlan.model_validate(plan("search", message, change("query", "moisturizer", "moisturizer"), change("need", "dry skin", "dry skin")))
    state, result = handle_turn(state.model_dump(), proposal, message, Search().search)
    assert result["products"] and state.active().sources["need"] == original
    assert all(change.field != "need" for change in proposal.changes)


def test_explicit_new_family_starts_fresh_even_if_model_labels_it_refinement():
    state = ShoppingState(tasks=[ShoppingTask(id=1, query="moisturizer",
        filters=ProductFilters(domain="beauty", family="skin_care", max_price="20"))], active_task_id=1)
    message = "Now a shirt"
    proposal = IntentPlan.model_validate(plan("refine", message, change("query", "shirt", "shirt")))
    search = Search()
    state, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "clarify" and not search.calls
    assert state.active().filters.family == "apparel" and state.active().filters.max_price is None
    assert state.tasks[0].filters.max_price == Decimal("20")


def test_literal_goal_recovery_and_missing_concern_evidence():
    message = "Now I also need a shirt"
    proposal = IntentPlan(intent="new_task", evidence=message)
    state, result = handle_turn({}, proposal, message, Search().search)
    assert state.active().query == "shirt" and result["action"] == "clarify"
    message = "moisturizer"
    proposal = IntentPlan.model_validate(plan("search", message, change("query", message, message)))
    row = product()
    row["listing_evidence"] = [{"text": "Example moisturizer", "field": "/title"}]
    state = ShoppingState(tasks=[ShoppingTask(id=1, need="dry skin")], active_task_id=1)
    _, result = handle_turn(state.model_dump(), proposal, message, lambda _: {"results": [row]})
    assert any("do not explicitly confirm suitability" in text for text in result["products"][0]["why_recommended"])


def test_reported_multi_concern_followup_accumulates_and_replays(tmp_path):
    first = "i need a prduct that lightens my face"
    second = "that lighten my face, reduce pimble, and so on"
    plans = {first: plan("clarify", first, change("need", "lightens my face", "lightens my face")),
             second: plan("clarify", second, change("need", "lighten my face", "lighten my face"),
                          change("need", "reduce pimble", "reduce pimble"), question_kind="multiple")}
    config, app, _ = harness(tmp_path, plans)
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        first_result = commerce(client, session, first)
        assert first_result["preferences"]["pending_kind"] == "skin_profile"
        result = commerce(client, session, second)
        assert result["action"] == "clarify" and "skin type" in result["reply"]
        assert "Which product" not in result["reply"]
        assert result["preferences"]["tasks"][0]["query"] == ""
        needs = result["preferences"]["tasks"][0]["needs"]
        assert [item["value"] for item in needs] == ["lightens my face", "lighten my face", "reduce pimble"]
        assert needs[-1]["evidence"] == "reduce pimble"
        repeated = commerce(client, session, second)
        assert repeated["preferences"]["tasks"][0]["needs"] == needs
        assert not search.calls
    with TestClient(create_app(config)) as restarted:
        saved = restarted.get(f"/conversations/{session}/preferences").json()
        assert saved["preferences"]["tasks"][0]["needs"] == needs
        assert len(restarted.get(f"/conversations/{session}/messages").json()["messages"]) == 6


@pytest.mark.parametrize("answer", ["oily skin under $20", "show options", "not sure, suggest something"])
def test_skin_concern_discovery_does_not_require_named_product_type(tmp_path, answer):
    first = "lighten my face and reduce pimble"
    plans = {first: plan("clarify", first, change("need", "lighten my face", "lighten my face"),
                         change("need", "reduce pimble", "reduce pimble")), answer: plan("refine", answer, recommendation_requested=True)}
    _, app, _ = harness(tmp_path, plans)
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        commerce(client, session, first)
        result = commerce(client, session, answer)
        assert result["action"] == "recommend" and result["products"]
        request = search.calls[-1]
        assert request.filters.family == "skin_care" and request.filters.domain == "beauty"
        assert "reduce pimble" in request.query and "lighten my face" in request.query
        assert request.filters.attributes == {}
        task = result["preferences"]["tasks"][0]
        assert task["query"] == "skin care"
        assert task["sources"]["query"]["specific_product_type_selected"] is False
        if "20" in answer:
            assert request.filters.max_price == Decimal("19.99")
            assert any(item["value"] == "oily skin" for item in task["needs"])


def test_legacy_concern_and_required_filters_survive_multi_need_update():
    state = ShoppingState(tasks=[ShoppingTask(id=1, query="moisturizer", need="dry skin",
        filters=ProductFilters(max_price="20", brand="Acme", attributes={"fragrance_free": True}),
        sources={"need": {"value": "dry skin", "evidence": "dry skin", "source": "user"}})], active_task_id=1)
    message = "also dark spots and uneven skin tone"
    proposal = IntentPlan.model_validate(plan("refine", message, change("need", "dark spots", "dark spots"),
        change("need", "uneven skin tone", "uneven skin tone")))
    search = Search()
    state, _ = handle_turn(state.model_dump(), proposal, message, search.search)
    assert [item.value for item in state.active().needs] == ["dry skin", "dark spots", "uneven skin tone"]
    assert state.active().needs[0].evidence == "dry skin"
    assert search.calls[-1].filters.max_price == Decimal("20")
    assert search.calls[-1].filters.brand == "Acme"
    assert search.calls[-1].filters.attributes == {"fragrance_free": True}


def test_multi_need_validation_still_rejects_invented_evidence_atomically(tmp_path):
    message = "lighten my face and reduce pimble"
    _, app, _ = harness(tmp_path, {message: plan("clarify", message, change("need", "lighten my face", "lighten my face"),
        change("need", "cure eczema", "cure eczema"))})
    with TestClient(app) as client:
        session = new(client)
        assert commerce(client, session, message)["validation_recovered"]
        assert client.get(f"/conversations/{session}/preferences").json()["preferences"]["tasks"] == []
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 2


def test_multiple_different_products_create_separate_groups():
    message = "I need shampoo and a moisturizer"
    proposal = IntentPlan.model_validate(plan("clarify", message, question_kind="multiple"))
    state, result = handle_turn({}, proposal, message, Search().search)
    assert result["action"] == "multi" and state.pending_kind == "task_selection"
    assert [task.query for task in state.tasks] == ["shampoo", "moisturizer"]
    assert [group["task_id"] for group in result["groups"]] == [1, 2]


def test_removing_one_concern_does_not_clear_the_others():
    message = "dry skin and dark spots"
    proposal = IntentPlan.model_validate(plan("clarify", message, change("need", "dry skin", "dry skin"), change("need", "dark spots", "dark spots")))
    state, _ = handle_turn({}, proposal, message, Search().search)
    message = "remove my dark spots concern"
    proposal = IntentPlan.model_validate(plan("refine", message, change("need", None, message)))
    state, _ = handle_turn(state.model_dump(), proposal, message, Search().search)
    assert [item.value for item in state.active().needs] == ["dry skin"]


def test_conflicting_scalar_filters_are_still_rejected(tmp_path):
    message = "under $20 or under $30"
    _, app, _ = harness(tmp_path, {message: plan("refine", message, change("max_price", "20", "under $20"), change("max_price", "30", "under $30"))})
    with TestClient(app) as client:
        session = new(client)
        assert commerce(client, session, message)["validation_recovered"]
        assert client.get(f"/conversations/{session}/preferences").json()["preferences"]["tasks"] == []


def test_generic_corrected_product_word_does_not_become_a_concrete_goal():
    message = "i need a prduct that lightens my face"
    proposal = IntentPlan.model_validate(plan("clarify", message, change("query", "product", "prduct"),
        change("need", "lightens my face", "lightens my face")))
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert result["action"] == "clarify" and state.pending_kind == "skin_profile"
    assert not state.active().query and state.active().need == "lightens my face" and not search.calls


def test_concern_mislabelled_as_query_is_saved_as_a_concern():
    message = "that lighten my face, reduce pimble, and so on"
    proposal = IntentPlan.model_validate(plan("refine", message, change("need", "reduce pimble", "reduce pimble"),
        change("query", "lighten my face", "that lighten my face")))
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert result["action"] == "clarify" and state.pending_kind == "skin_profile"
    assert {item.value for item in state.active().needs} == {"lighten my face", "reduce pimble"}
    assert not state.active().query and not search.calls


@pytest.mark.parametrize("inferred", ["skin care", "serum", "face cream"])
def test_inferred_product_type_is_not_invented_from_a_literal_skin_concern(inferred):
    message = "lighten my face"
    proposal = IntentPlan.model_validate(plan("search", message, change("query", inferred, message)))
    search = Search()
    state, result = handle_turn({}, proposal, message, search.search)
    assert state.active().need == message and state.active().query == ""
    assert result["action"] == "clarify" and not search.calls


def test_one_repair_attempt_revalidates_without_saving_partial_state(tmp_path):
    message = "moisturizer under $20"
    answers = [plan("search", message, change("query", "moisturizer", "moisturizer"), change("brand", "Invented", "moisturizer")),
               plan("search", message, change("query", "moisturizer", "moisturizer"))]
    _, app, calls = harness(tmp_path, {message: answers})
    with TestClient(app) as client:
        search = app.state.product_search = Search()
        session = new(client)
        result = commerce(client, session, message)
        assert result["intent_attempts"] == 2 and len(calls) == 2 and len(search.calls) == 1
        assert any("Validation feedback" in item["content"] for item in calls[-1]["messages"] if item["role"] == "system")
        assert result["preferences"]["tasks"][0]["filters"]["brand"] is None
        assert search.calls[-1].filters.max_price == Decimal("19.99")
        assert len(client.get(f"/conversations/{session}/messages").json()["messages"]) == 2
        assert client.get(f"/conversations/{session}/preferences").json()["state_version"] == 1


def test_repeated_concern_wrongly_labelled_reset_preserves_task():
    message = "lighten my face, reduce pimble"
    state, _ = handle_turn({}, IntentPlan.model_validate(plan("clarify", message,
        change("need", "lighten my face", "lighten my face"), change("need", "reduce pimble", "reduce pimble"))), message, Search().search)
    state.active().filters.max_price = Decimal("20")
    proposal = IntentPlan(intent="reset", evidence=message)
    state, result = handle_turn(state.model_dump(), proposal, message, Search().search)
    assert result["action"] == "clarify" and len(state.tasks) == 1
    assert len(state.active().needs) == 2 and state.active().filters.max_price == Decimal("20")


def test_profile_answer_ignores_old_subspan_restatements_and_inferred_taxonomy():
    message = "lighten my face and reduce pimble"
    state, _ = handle_turn({}, IntentPlan.model_validate(plan("clarify", message, change("need", message, message))), message, Search().search)
    message = "oily skin under $20 show options"
    proposal = IntentPlan.model_validate(plan("refine", message,
        change("need", "lighten my face", "lighten my face"),
        change("domain", "beauty", "oily skin"), change("family", "skin_care", "oily skin")))
    search = Search()
    state, result = handle_turn(state.model_dump(), proposal, message, search.search)
    assert result["action"] == "recommend" and search.calls[-1].filters.domain == "beauty"
    assert search.calls[-1].filters.family == "skin_care" and search.calls[-1].filters.max_price == Decimal("19.99")
    assert state.active().needs[0].value == "lighten my face and reduce pimble"
    assert state.active().sources["domain"]["source"] == "concern_guided_discovery"


def test_ambiguous_concern_removal_with_exception_keeps_all_until_clarified():
    message = "dry skin and dark spots"
    state, _ = handle_turn({}, IntentPlan.model_validate(plan("clarify", message, change("need", "dry skin", "dry skin"),
        change("need", "dark spots", "dark spots"))), message, Search().search)
    message = "remove all concerns except dry skin"
    state, result = handle_turn(state.model_dump(), IntentPlan.model_validate(plan("refine", message, change("need", None, message))), message, Search().search)
    assert result["action"] == "clarify" and {item.value for item in state.active().needs} == {"dry skin", "dark spots"}


def test_legacy_generic_question_does_not_lose_a_new_issue_when_model_omits_changes():
    state = ShoppingState(tasks=[ShoppingTask(id=1, need="lightens my face")], active_task_id=1,
        pending_kind="product_type", pending_question="What type of product are you looking for?")
    message = "that lighten my face, reduce pimble, and so on"
    proposal = IntentPlan(intent="clarify", evidence=message, question_kind="multiple")
    state, result = handle_turn(state.model_dump(), proposal, message, Search().search)
    assert result["action"] == "clarify" and state.pending_kind == "skin_profile"
    assert "lightens my face" in state.active().need and "reduce pimble" in state.active().need
    assert state.active().needs[-1].value == message and state.active().needs[-1].evidence == message

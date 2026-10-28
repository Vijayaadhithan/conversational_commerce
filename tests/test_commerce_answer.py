"""Natural responses are reviewed before display and retain atomic replay/state contracts."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, new, plan
from test_commerce_feedback import seeded
from test_commerce_stream import frames

from app.commerce_answer import natural_answer
from app.commerce_answer_context import GroundedAnswer, answer_facts, validate_answer
from app.commerce_discovery import question
from app.commerce_models import ShoppingState
from app.config import Settings
from app.main import create_app

APPROVED = {"supported": True, "answers_request": True, "preserves_action": True}


def sentence(text, *ids):
    return {"sentences": [{"text": text, "evidence_ids": list(ids)}]}


def fake_chat(responses, **settings):
    calls = []

    async def request(method, path, **kwargs):
        calls.append(kwargs["json"])
        value = responses[len(calls) - 1]
        if isinstance(value, Exception):
            raise value
        return {
            "done": True,
            "done_reason": "stop",
            "message": {"content": json.dumps(value)},
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

    return SimpleNamespace(settings=replace(Settings(), **settings), model_request=request), calls


def recommendation():
    state, _ = seeded()
    return state, {
        "action": "recommend",
        "reply": "Original grounded result",
        "products": state.active().shown,
        "search": {},
    }


def test_generated_answer_has_allowlisted_evidence_and_code_rendered_products():
    state, value = recommendation()
    chat, calls = fake_chat(
        [sentence("Option 1 has related listing text; suitability is unverified.", "p1"), APPROVED]
    )
    before = state.model_dump()
    usage = asyncio.run(natural_answer(chat, state, "Why these?", value))
    assert value["answer_generation"]["status"] == "generated" and len(calls) == len(usage) == 2
    citation_schema = calls[0]["format"]["$defs"]["AnswerSentence"]["properties"]["evidence_ids"]
    assert citation_schema["items"]["enum"] == list(answer_facts(state, value))
    assert "1. Moisturizer for dry skin 1" in value["reply"] and "$5 USD" in value["reply"]
    assert state.model_dump() == before
    assert (
        value["answer_generation"]["evidence"]["p1"]["product_id"]
        == state.active().shown[0]["product_id"]
    )


@pytest.mark.parametrize(
    "candidate",
    [
        sentence("This costs $9999.", "p1"),
        sentence("Buy it at https://fake.example", "p1"),
        sentence("It cures everything.", "made-up"),
        {"sentences": [{"text": "hello", "evidence_ids": []}]},
    ],
)
def test_invalid_evidence_numbers_links_and_schema_fall_back_before_review(candidate):
    state, value = recommendation()
    chat, calls = fake_chat([candidate])
    asyncio.run(natural_answer(chat, state, "help", value))
    assert value["reply"] == "Original grounded result" and len(calls) == 1
    assert value["answer_generation"]["status"] == "fallback"


@pytest.mark.parametrize("field", list(APPROVED))
def test_semantic_review_rejection_never_changes_reply_or_state(field):
    state, value = recommendation()
    chat, _ = fake_chat(
        [sentence("This is excellent for sensitive skin.", "p1"), {**APPROVED, field: False}]
    )
    before = state.model_dump()
    asyncio.run(natural_answer(chat, state, "help", value))
    assert value["reply"] == "Original grounded result" and state.model_dump() == before
    assert value["answer_generation"]["failure_stage"] == "review_verdict"


def test_budget_and_timeout_fall_back_without_partial_text():
    state, value = recommendation()
    chat, calls = fake_chat([], context_size=2100)
    asyncio.run(natural_answer(chat, state, "help", value))
    assert not calls and value["reply"] == "Original grounded result"
    assert value["answer_generation"]["failure_stage"] == "generation_context"
    chat, calls = fake_chat([TimeoutError("private error")])
    asyncio.run(natural_answer(chat, state, "help", value))
    assert len(calls) == 1 and "private" not in json.dumps(value)
    assert value["answer_generation"]["failure_stage"] == "generation_request"


def test_natural_question_is_saved_as_the_actual_pending_question():
    state = ShoppingState()
    value = question(state, "Who are these for?", "recipient")
    chat, _ = fake_chat([sentence("Who will be wearing these?", "result"), APPROVED])
    asyncio.run(natural_answer(chat, state, "I'd like shoes", value))
    assert state.pending_question == value["reply"] == "Who will be wearing these?"
    assert state.pending_kind == "recipient"


def test_protected_removal_question_never_goes_through_generation():
    state = ShoppingState()
    value = question(state, "Continue without the unsupported requirement?", "unsupported")
    chat, calls = fake_chat([])
    assert asyncio.run(natural_answer(chat, state, "yes", value)) == []
    assert not calls and value["answer_generation"]["status"] == "not_applicable"


def test_one_question_limit_is_checked_without_trusting_the_reviewer():
    state = ShoppingState()
    value = question(state, "Who is it for?", "recipient")
    with pytest.raises(ValueError):
        validate_answer(
            GroundedAnswer.model_validate(sentence("Who is it for? What size?", "result")),
            answer_facts(state, value),
            "clarify",
        )


@pytest.mark.parametrize("provider", ["ollama", "google", "groq", "openrouter"])
@pytest.mark.parametrize("accepted", [True, False])
def test_natural_sse_replay_and_history_use_only_the_reviewed_reply(
    tmp_path, monkeypatch, provider, accepted
):
    calls = []
    if provider != "ollama":
        monkeypatch.setenv(provider.upper() + "_API_KEY", "test-only-key")
        monkeypatch.delenv(provider.upper() + "_API_KEY_FILE", raising=False)

    def handler(request):
        body = json.loads(request.content)
        title = (
            body["format"]["title"]
            if provider == "ollama"
            else json.loads(body["messages"][0]["content"].split("JSON Schema: ", 1)[1])["title"]
        )
        calls.append(title)
        value = {
            "IntentPlan": plan("search", "moisturizer", recommendation_requested=True),
            "GroundedAnswer": sentence(
                "Option 1 has related listing wording; suitability is unverified.", "p1"
            ),
            "AnswerReview": {**APPROVED, "supported": accepted},
        }[title]
        if provider == "ollama":
            return httpx.Response(
                200, json={"done": True, "message": {"content": json.dumps(value)}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]
            },
        )

    config = replace(
        Settings(),
        database=str(tmp_path / "natural.sqlite3"),
        commerce_natural=True,
        provider=provider,
    )
    with TestClient(create_app(config, httpx.MockTransport(handler))) as client:
        client.app.state.product_search = Search()
        cid = new(client)
        url = f"/conversations/{cid}/messages"
        headers = {"Accept": "text/event-stream", "Idempotency-Key": "natural-replay"}
        events = frames(client.post(url, json={"message": "moisturizer"}, headers=headers).text)
        assert events[-1][0] == "done", events
        result = events[-1][1]
        assert result["commerce"]["answer_generation"]["status"] == (
            "generated" if accepted else "fallback"
        )
        if not accepted:
            assert "Option 1 has related listing wording" not in json.dumps(events)
        assert "".join(data["text"] for name, data in events if name == "delta") == result["reply"]
        replay = client.post(url, json={"message": "moisturizer"}, headers=headers)
        assert frames(replay.text)[-1][1] == result
        assert calls == ["IntentPlan", "GroundedAnswer", "AnswerReview"]
        history = client.get(url).json()["messages"]
        assert history[-1]["content"] == result["reply"] and len(history) == 2

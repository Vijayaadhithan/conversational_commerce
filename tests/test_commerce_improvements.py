"""Intent recovery, business scope, grounded live streaming, and durable fallbacks."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_commerce import Search, change, harness, new, plan, send
from test_commerce_stream import frames

from app.commerce_stream import ProgressQueue, stream_turn
from app.commerce_summary import add_summary
from app.config import Settings
from app.main import create_app
from app.operations import Metrics
from app.summary_stream import SummaryStreamError


def test_malformed_then_valid_plan_repairs_once(tmp_path):
    message = "moisturizer under $30"
    _, app, calls = harness(
        tmp_path,
        {
            message: [
                "broken",
                plan("search", message, change("query", "moisturizer", "moisturizer")),
            ]
        },
    )
    with TestClient(app) as client:
        app.state.product_search = search = Search()
        result = send(client, new(client), message).json()
        assert result["commerce"]["action"] == "recommend"
        assert result["commerce"]["intent_attempts"] == 2
        assert len(calls) == 2 and len(search.calls) == 1
        assert "valid IntentPlan JSON" in str(calls[-1]["messages"])


def test_transport_failure_is_not_retried_as_intent(tmp_path):
    _, app, calls = harness(tmp_path, {"moisturizer": httpx.ConnectError("private")})
    with TestClient(app) as client:
        cid = new(client)
        response = send(client, cid, "moisturizer")
        assert response.status_code == 503 and "private" not in response.text
        assert len(calls) == 1
        assert client.get(f"/conversations/{cid}/messages").json()["messages"] == []


def test_scope_redirect_preserves_state_and_does_not_search(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMERCE_ONLY", "1")
    message = "write Python code about shampoo"
    _, app, calls = harness(
        tmp_path,
        {
            message: plan(
                "search",
                message,
                change("query", "shampoo", "shampoo"),
                scope="off_topic",
                recommendation_requested=True,
            )
        },
    )
    with TestClient(app) as client:
        app.state.product_search = search = Search()
        created = client.post("/conversations", json={}).json()
        assert created["mode"] == "commerce"
        cid = created["id"]
        state = client.get(f"/conversations/{cid}/preferences").json()["preferences"]
        response = send(client, cid, message).json()["commerce"]
        assert response["policy_redirect"] and response["scope"] == "off_topic"
        assert response["intent"]["changes"] == []
        assert not search.calls and len(calls) == 1
        assert client.get(f"/conversations/{cid}/preferences").json()["preferences"] == state
        assert client.post("/conversations", json={"mode": "chat"}).status_code == 422


def test_commerce_only_blocks_old_chat_without_deleting_it(tmp_path):
    settings = replace(Settings(), database=str(tmp_path / "chat.sqlite3"), commerce_only=False)
    with TestClient(create_app(settings)) as client:
        cid = client.post("/conversations", json={"mode": "chat"}).json()["id"]
    with TestClient(create_app(replace(settings, commerce_only=True))) as client:
        assert send(client, cid, "write code").status_code == 409
        assert client.get(f"/conversations/{cid}").status_code == 200
        assert client.get(f"/conversations/{cid}/messages").json()["messages"] == []


@pytest.mark.parametrize("value", ["yes", "", "2"])
def test_policy_flags_reject_ambiguous_configuration(monkeypatch, value):
    monkeypatch.setenv("COMMERCE_ONLY", value)
    with pytest.raises(ValueError, match="COMMERCE_ONLY"):
        Settings()


def sample_commerce():
    return {
        "reply": "Original safe reply",
        "products": [
            {
                "product_id": "amazon:B000000001",
                "title": "Dry skin moisturizer",
                "why_recommended": [
                    "The listing mentions dry skin.",
                    "Its historical price is $12 USD.",
                ],
            }
        ],
    }


def test_summary_selects_only_allowlisted_facts_and_keeps_safe_prefix_on_failure(monkeypatch):
    from app import commerce_summary

    async def fake(*args):
        yield '{"facts":["f1"', None
        raise SummaryStreamError("secret provider details")

    monkeypatch.setattr(commerce_summary, "selection_stream", fake)
    chat = SimpleNamespace(settings=Settings())
    commerce = sample_commerce()
    output = []
    asyncio.run(
        add_summary(chat, "dry skin", commerce, lambda name, data: output.append(data["text"]))
    )
    assert commerce["summary"]["status"] == "partial_fallback"
    assert commerce["reply"] == "".join(output) + "\nOriginal safe reply"
    assert "secret" not in commerce["reply"]
    assert commerce["summary"]["evidence"][0]["product_id"] == "amazon:B000000001"


@pytest.mark.parametrize(
    "selection", ['{"facts":["invented"]}', '{"facts":[],"answer":"guaranteed cure"}', "not JSON"]
)
def test_invalid_summary_falls_back_without_generated_claims(monkeypatch, selection):
    from app import commerce_summary

    async def fake(*args):
        yield selection, None

    monkeypatch.setattr(commerce_summary, "selection_stream", fake)
    commerce = sample_commerce()
    asyncio.run(add_summary(SimpleNamespace(settings=Settings()), "dry skin", commerce))
    assert commerce["reply"] == "Original safe reply"
    assert commerce["summary"]["status"] == "fallback"


def test_stream_delivers_highlight_before_completion_and_no_duplicate_on_fast_finish():
    async def check():
        queue, release = ProgressQueue(), asyncio.Event()

        async def work():
            queue.put_nowait(("delta", {"text": "Verified highlight\n"}))
            await release.wait()
            # Queue another fragment immediately before completion to exercise the race.
            queue.put_nowait(("delta", {"text": "Second highlight\n"}))
            return {"reply": "Verified highlight\nSecond highlight\nFinal reply"}

        stream = stream_turn(work, queue)
        await anext(stream)
        early = await anext(stream)
        assert "Verified highlight" in early and not release.is_set()
        release.set()
        result = frames(early + "".join([item async for item in stream]))
        assert result[-1][0] == "done"
        assert (
            "".join(data["text"] for name, data in result if name == "delta")
            == result[-1][1]["reply"]
        )

    asyncio.run(check())


@pytest.mark.parametrize("provider", ["ollama", "groq"])
def test_stream_protocol_and_final_usage(provider):
    from app.summary_stream import selection_stream

    async def check():
        def handler(request):
            assert json.loads(request.content)["stream"] is True
            if provider == "ollama":
                return httpx.Response(
                    200,
                    text=json.dumps({"message": {"content": '{"facts":["f1"]}'}, "done": False})
                    + "\n"
                    + json.dumps(
                        {
                            "message": {"content": ""},
                            "done": True,
                            "done_reason": "stop",
                            "eval_count": 8,
                            "prompt_eval_count": 20,
                        }
                    )
                    + "\n",
                )
            return httpx.Response(
                200,
                text="data: "
                + json.dumps(
                    {"choices": [{"delta": {"content": '{"facts":["f1"]}'}, "finish_reason": None}]}
                )
                + "\n\ndata: "
                + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                + "\n\ndata: [DONE]\n\n",
            )

        async with httpx.AsyncClient(
            base_url="http://fixture", transport=httpx.MockTransport(handler)
        ) as client:
            chat = SimpleNamespace(
                settings=replace(Settings(), provider=provider),
                state=SimpleNamespace(ollama=client, metrics=Metrics()),
            )
            pieces = [item async for item in selection_stream(chat, [], {})]
            assert "".join(piece[0] for piece in pieces) == '{"facts":["f1"]}'
            assert pieces[-1][1] is not None

    asyncio.run(check())


def test_summary_sse_replay_and_history_are_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMERCE_SUMMARY_ENABLED", "1")
    calls = []
    message = "moisturizer under $30"

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body.get("stream"):
            lines = [
                {"message": {"content": '{"facts":["f1"]}'}, "done": False},
                {"message": {"content": ""}, "done": True, "done_reason": "stop"},
            ]
            return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines) + "\n")
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {
                    "content": json.dumps(
                        plan("search", message, change("query", "moisturizer", "moisturizer"))
                    )
                },
            },
        )

    app = create_app(
        replace(Settings(), database=str(tmp_path / "stream.sqlite3")), httpx.MockTransport(handler)
    )
    with TestClient(app) as client:
        app.state.product_search = Search()
        cid = new(client)
        url = f"/conversations/{cid}/messages"
        headers = {"Idempotency-Key": "summary-replay", "Accept": "text/event-stream"}
        events = frames(client.post(url, json={"message": message}, headers=headers).text)
        assert events[-1][0] == "done", events
        response = events[-1][1]
        assert response["commerce"]["summary"]["status"] == "generated"
        assert any(name == "commerce" and value.get("provisional") for name, value in events)
        assert (
            "".join(data["text"] for name, data in events if name == "delta") == response["reply"]
        )
        replay = client.post(url, json={"message": message}, headers=headers)
        assert frames(replay.text)[-1] == ("done", response) and len(calls) == 2
        history = client.get(url).json()["messages"]
        assert len(history) == 2 and history[-1]["content"] == response["reply"]


def test_explicit_handoff_avoids_scope_misclassification_and_model_calls(tmp_path):
    _, app, calls = harness(tmp_path, {})
    with TestClient(app) as client:
        cid = client.post(
            "/conversations", json={"mode": "commerce", "storefront_id": "beauty"}
        ).json()["id"]
        before = client.get(f"/conversations/{cid}/preferences").json()["preferences"]
        message = "Recommend a cotton shirt for adult men under $40"
        value = send(client, cid, message).json()["commerce"]
        assert value["action"] == "handoff" and not calls
        assert value["handoff"]["targets"][0]["message"] == message
        assert value["handoff"]["targets"][0]["storefront_id"] == "clothing"
        assert client.get(f"/conversations/{cid}/preferences").json()["preferences"] == before


@pytest.mark.parametrize(
    "message",
    [
        "Do not recommend a shirt",
        "Write code to recommend a shirt",
        "Do I need a shirt?",
        "Recommend not a shirt but a bag",
        'Please say "recommend a shirt"',
    ],
)
def test_handoff_does_not_infer_purchase_from_negation_or_quoted_words(message):
    from app.commerce_models import ShoppingState
    from app.commerce_policy import handoff_plan

    assert handoff_plan(ShoppingState(storefront_id="beauty"), message) is None


def test_metal_appearance_is_not_an_invented_style_filter(tmp_path):
    message = "Recommend a silver necklace under $50"
    _, app, calls = harness(
        tmp_path,
        {
            message: [
                plan(
                    "search",
                    message,
                    change("query", "necklace", "necklace"),
                    change("attribute.style", "silver", "silver"),
                ),
                plan("search", message, change("query", "silver necklace", "silver necklace")),
            ]
        },
    )
    with TestClient(app) as client:
        app.state.product_search = search = Search()
        result = send(client, new(client), message).json()["commerce"]
        assert result["intent_attempts"] == 1 and len(calls) == 1
        assert search.calls[0].query == "silver necklace"
        assert search.calls[0].filters.attributes == {}


def test_repair_null_for_unset_attribute_cannot_clear_an_existing_requirement(tmp_path):
    message = "Recommend a silver necklace under $50"
    _, app, _ = harness(
        tmp_path,
        {
            message: plan(
                "search",
                message,
                change("query", "silver necklace", "silver necklace"),
                change("attribute.style", None, "silver"),
            )
        },
    )
    with TestClient(app) as client:
        app.state.product_search = search = Search()
        result = send(client, new(client), message).json()["commerce"]
        assert result["action"] == "recommend" and search.calls[0].filters.attributes == {}


def test_stream_tracks_a_delta_already_dequeued_by_the_consumer():
    async def check():
        queue = ProgressQueue()
        queue.put_nowait(("delta", {"text": "In flight\n"}))
        in_flight = await queue.get()

        async def work():
            return {"reply": "In flight\nCommitted tail"}

        events = frames("".join([frame async for frame in stream_turn(work, queue)]))
        rest = "".join(data["text"] for name, data in events if name == "delta")
        assert in_flight[1]["text"] + rest == events[-1][1]["reply"]
        assert rest == "Committed tail"

    asyncio.run(check())


def test_provisional_stream_failure_never_emits_done():
    from fastapi import HTTPException

    async def check():
        queue = ProgressQueue()

        async def work():
            queue.put_nowait(("delta", {"text": "Validated but not saved"}))
            await asyncio.sleep(0)
            raise HTTPException(503, "private persistence failure")

        events = frames("".join([frame async for frame in stream_turn(work, queue)]))
        assert events[-1][0] == "error" and not any(name == "done" for name, _ in events)
        assert "private persistence" not in str(events)

    asyncio.run(check())

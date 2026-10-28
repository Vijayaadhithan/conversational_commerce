"""Natural, evidence-cited prose with bounded generation and a fail-closed review."""

import asyncio
import json

from fastapi import HTTPException

from app.catalog import strict_json
from app.chat_service import compact_schema
from app.commerce_answer_context import (
    REVIEW_PROMPT,
    WRITE_PROMPT,
    AnswerReview,
    GroundedAnswer,
    answer_facts,
    product_footer,
    validate_answer,
)
from app.text_budget import byte_size, clip

PROTECTED_QUESTIONS = {
    "unsupported",
    "budget_conflict",
    "intent_repair",
    "evidence_limit",
    "task_selection",
    "multiple",
    "feedback_limit",
    "query_size",
}


async def natural_answer(chat, state, message, commerce, dialogue=()):
    """Return usage for at most two calls; never expose unchecked generated fragments."""
    if (
        commerce.get("validation_recovered")
        or commerce.get("policy_redirect")
        or commerce["action"]
        not in {
            "recommend",
            "details",
            "compare",
            "answer",
            "clarify",
            "smalltalk",
            "preferences",
            "multi",
        }
        or (commerce["action"] == "clarify" and state.pending_kind in PROTECTED_QUESTIONS)
        or (commerce["action"] == "clarify" and state.pending_question != commerce["reply"])
    ):
        commerce["answer_generation"] = {"status": "not_applicable"}
        return []
    facts = answer_facts(state, commerce)
    context = {"latest_message": message, "facts": facts}
    usage = []

    async def request(prompt, payload, model, tokens):
        schema = compact_schema(model.model_json_schema())
        schema["title"] = model.__name__
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        if byte_size(messages) + byte_size(schema) > chat.settings.prompt_budget:
            raise ValueError("Answer context budget")
        # Dialogue helps tone/continuity but is never part of the evidence map.
        if model is GroundedAnswer and dialogue:
            extra = [
                {"role": item["role"], "content": clip(item["content"], 500)}
                for item in dialogue[-2:]
            ]
            enriched = {**payload, "recent_dialogue": extra}
            proposed = messages[:-1] + [
                {"role": "user", "content": json.dumps(enriched, ensure_ascii=False)}
            ]
            if byte_size(proposed) + byte_size(schema) <= chat.settings.prompt_budget:
                messages = proposed
        result = await chat.model_request(
            "POST",
            "/api/chat",
            json={
                "model": chat.settings.model,
                "messages": messages,
                "format": schema,
                "stream": False,
                "keep_alive": chat.settings.keep_alive,
                "options": {
                    "temperature": 0,
                    "num_ctx": chat.settings.context_size,
                    "num_predict": min(tokens, chat.settings.max_tokens),
                },
            },
        )
        usage.append(result)
        if result.get("done") is not True or result.get("done_reason") == "length":
            raise ValueError("Incomplete answer")
        return model.model_validate(strict_json(result["message"]["content"]))

    try:
        async with asyncio.timeout(min(chat.settings.timeout, 30)):
            answer = await request(WRITE_PROMPT, context, GroundedAnswer, 700)
            text = validate_answer(answer, facts, commerce["action"])
            review = await request(
                REVIEW_PROMPT, {**context, "candidate": answer.model_dump()}, AnswerReview, 128
            )
            if not (review.supported and review.answers_request and review.preserves_action):
                raise ValueError("Answer review rejected")
    except (HTTPException, TimeoutError, ValueError, KeyError, TypeError):
        commerce["answer_generation"] = {
            "status": "fallback",
            "reason": "generation_or_review_failed",
        }
        # Interrupted inference can have unreported usage; never pretend it was free.
        usage.append({})
        return usage
    footer = product_footer(commerce) if commerce.get("products") or commerce.get("groups") else ""
    original = commerce["reply"]
    commerce["reply"] = text + ("\n\n" + footer if footer else "")
    if commerce["action"] == "clarify" and state.pending_question == original:
        state.pending_question = commerce["reply"]
        if state.active():
            state.active().pending_question = commerce["reply"]
    commerce["answer_generation"] = {
        "status": "generated",
        "method": "evidence_cited_model_review",
        "evidence": {key: facts[key] for item in answer.sentences for key in item.evidence_ids},
    }
    return usage

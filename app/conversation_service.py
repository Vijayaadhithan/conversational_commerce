"""Validated chat and shopping turns with atomic persistence."""

import asyncio
import logging
from collections.abc import Callable
from decimal import InvalidOperation

import psycopg
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import State

from app.api_models import ChatRequest
from app.catalog import CatalogError
from app.chat_service import ChatService
from app.commerce import IntentError, direct_plan, intent_clarification
from app.commerce_models import IntentPlan, ShoppingState, preference_view
from app.config import Settings

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(
        self, state: State, settings: Settings, chat: ChatService, turn_handler: Callable
    ) -> None:
        self.state = state
        self.settings = settings
        self.chat = chat
        self.handle_turn = turn_handler

    async def process_message(self, key: str, body: ChatRequest, user_id: str, identity) -> dict:
        record = self.state.store.check_owner(key, user_id)
        if record["mode"] == "commerce":
            state = ShoppingState.model_validate(record["shopping_state"])
            repair_reason, inference = "", []
            attempts = 1 if direct_plan(body.message) else 2
            for attempt in range(attempts):
                plan, result = await self.chat.shopping_plan(state, body.message, repair_reason)
                inference.append(result)
                try:
                    next_state, commerce = await asyncio.to_thread(
                        self.handle_turn,
                        state.model_dump(mode="json"),
                        plan,
                        body.message,
                        self.state.product_search.search,
                    )
                except (IntentError, ValidationError, InvalidOperation) as exc:
                    repair_reason = (
                        str(exc)
                        if isinstance(exc, IntentError)
                        else "Preference schema or price validation failed"
                    )
                    logger.warning(
                        "Shopping intent rejected (attempt %s): %s", attempt + 1, repair_reason
                    )
                    if attempt + 1 < attempts:
                        continue
                    next_state, commerce = intent_clarification(
                        state.model_dump(mode="json"), body.message
                    )
                    state = next_state
                    # The rejected proposal is not a validated intent or a state patch.
                    plan = IntentPlan(
                        intent="clarify", evidence=body.message[:500], question_kind="general"
                    )
                    break
                except CatalogError as exc:
                    raise HTTPException(503, str(exc)) from exc
                except (psycopg.Error, OSError) as exc:
                    raise HTTPException(
                        503, "Product search unavailable; shopping turn was not saved"
                    ) from exc
                state = next_state
                break
            commerce.update(
                intent=plan.model_dump(mode="json"),
                preferences=preference_view(state),
                state_version=record["state_version"] + 1,
                intent_attempts=len(inference),
            )
            response = {
                "conversation_id": key,
                "reply": commerce["reply"],
                "model": self.settings.model,
                "done_reason": result.get("done_reason", "deterministic"),
                "commerce": commerce,
                "usage": {
                    "prompt_tokens": sum(item["prompt_eval_count"] for item in inference)
                    if all(isinstance(item.get("prompt_eval_count"), int) for item in inference)
                    else None,
                    "completion_tokens": sum(item["eval_count"] for item in inference)
                    if all(isinstance(item.get("eval_count"), int) for item in inference)
                    else None,
                },
            }
            self.state.store.commit_commerce_turn(
                key, body.message, state, commerce, record["state_version"], identity, response
            )
            return response
        history = self.state.store.messages(key, record["summarized_through"], limit=1000)
        messages, summary, boundary = await self.chat.prepare_context(record, history, body.message)
        answer, result = await self.chat.complete(messages)
        response = {
            "conversation_id": key,
            "reply": answer,
            "model": self.settings.model,
            "done_reason": result.get("done_reason"),
            "usage": {
                "prompt_tokens": result.get("prompt_eval_count"),
                "completion_tokens": result.get("eval_count"),
            },
        }

        self.state.store.commit_turn(
            key, body.message, answer, summary, boundary, identity, response
        )
        return response

"""Commerce intent validation, search coordination, and atomic turn persistence."""

import asyncio
import logging
from collections.abc import Callable
from decimal import InvalidOperation

import psycopg
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import State

from app.catalog import CatalogError
from app.chat_service import ChatService
from app.commerce_discovery import intent_clarification
from app.commerce_errors import IntentError, InvalidShoppingIntent
from app.commerce_intent import direct_plan
from app.commerce_models import IntentPlan, preference_view, shopping_state_for
from app.commerce_policy import scope_reply
from app.commerce_storefront import TurnHandler, handle_storefront_turn
from app.commerce_summary import add_summary
from app.config import Settings
from app.inference_usage import aggregate_usage

logger = logging.getLogger(__name__)


class CommerceTurnService:
    def __init__(
        self, state: State, settings: Settings, chat: ChatService, turn_handler: TurnHandler
    ) -> None:
        self.state = state
        self.settings = settings
        self.chat = chat
        self.handle_turn = turn_handler

    async def process_message(
        self,
        record: dict,
        message: str,
        identity,
        progress: Callable[[str], None] | None = None,
        emit=None,
    ) -> dict:
        key = record["id"]
        state = shopping_state_for(record)
        repair_reason, inference, result = "", [], {}
        attempts = 1 if direct_plan(message) else 2
        for attempt in range(attempts):
            if progress:
                progress(
                    "Understanding your request…" if not attempt else "Checking your preferences…"
                )

            def search(request):
                if progress:
                    progress("Finding products that match your preferences…")
                return self.state.product_search.search(request)

            try:
                plan, result = await self.chat.shopping_plan(state, message, repair_reason)
                inference.append(result)
                scoped = scope_reply(state, plan, message)
                if scoped:
                    state, commerce = scoped
                    plan = IntentPlan(
                        intent="clarify",
                        scope=plan.scope,
                        evidence=message[:500],
                        recommendation_requested=False,
                    )
                    break
                next_state, commerce = await asyncio.to_thread(
                    handle_storefront_turn,
                    state.model_dump(mode="json"),
                    plan,
                    message,
                    search,
                    self.handle_turn,
                )
            except (InvalidShoppingIntent, IntentError, ValidationError, InvalidOperation) as exc:
                if isinstance(exc, InvalidShoppingIntent):
                    result = exc.result
                    inference.append(result)
                repair_reason = (
                    str(exc)
                    if isinstance(exc, (IntentError, InvalidShoppingIntent))
                    else "Preference schema or price validation failed"
                )
                logger.warning(
                    "Shopping intent rejected (attempt %s): %s", attempt + 1, repair_reason
                )
                if attempt + 1 < attempts:
                    continue
                next_state, commerce = intent_clarification(state.model_dump(mode="json"), message)
                state = next_state
                # The rejected proposal is not a validated intent or a state patch.
                plan = IntentPlan(intent="clarify", evidence=message[:500], question_kind="general")
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
        if self.settings.commerce_summary and (commerce.get("products") or commerce.get("groups")):
            if progress:
                progress("Preparing highlights from the product listings…")
            if emit:
                emit("commerce", {**commerce, "provisional": True})
            summary_usage = await add_summary(self.chat, message, commerce, emit)
            if summary_usage is not None:
                inference.append(summary_usage)
        response = {
            "conversation_id": key,
            "reply": commerce["reply"],
            "model": self.settings.model,
            "done_reason": result.get("done_reason", "deterministic"),
            "commerce": commerce,
            "usage": aggregate_usage(inference),
        }
        self.state.store.commit_commerce_turn(
            key, message, state, commerce, record["state_version"], identity, response
        )
        return response

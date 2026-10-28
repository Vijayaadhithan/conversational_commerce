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
from app.commerce_answer import natural_answer
from app.commerce_discovery import intent_clarification
from app.commerce_errors import IntentError, InvalidShoppingIntent
from app.commerce_intent import direct_plan
from app.commerce_models import IntentPlan, preference_view, shopping_state_for
from app.commerce_performance import StageTimer, accumulate_search_stages, response_route
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
        timings = StageTimer()
        search_stages, search_calls = {}, 0
        with timings.measure("context_seconds"):
            key = record["id"]
            state = shopping_state_for(record)
            dialogue = self.state.store.shopping_dialogue(key)
        repair_reason, inference, result = "", [], {}
        attempts = 1 if direct_plan(message) else 2
        for attempt in range(attempts):
            if progress:
                progress(
                    "Understanding your request…" if not attempt else "Checking your preferences…"
                )

            def search(request):
                nonlocal search_calls
                search_calls += 1
                if progress:
                    progress("Finding products that match your preferences…")
                with timings.measure("search_seconds"):
                    searched = self.state.product_search.search(request)
                accumulate_search_stages(search_stages, searched)
                return searched

            try:
                with timings.measure("intent_seconds"):
                    plan, result = await self.chat.shopping_plan(
                        state, message, repair_reason, dialogue=dialogue
                    )
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
        products_ready = timings.elapsed() if commerce.get("products") else None
        route = response_route(self.settings, message, commerce)
        commerce["response_route"] = route
        if route["mode"] == "fast":
            commerce["answer_generation"] = {"status": "skipped", "reason": "fast_path"}
        elif self.settings.commerce_natural:
            if progress:
                progress("Preparing your answer…")
            if emit and (commerce.get("products") or commerce.get("groups")):
                emit("commerce", {**commerce, "provisional": True})
            inference.extend(
                await natural_answer(self.chat, state, message, commerce, dialogue, timings=timings)
            )
            commerce["preferences"] = preference_view(state)
        elif self.settings.commerce_summary and (
            commerce.get("products") or commerce.get("groups")
        ):
            if progress:
                progress("Preparing highlights from the product listings…")
            if emit:
                emit("commerce", {**commerce, "provisional": True})
            with timings.measure("summary_seconds"):
                summary_usage = await add_summary(self.chat, message, commerce, emit)
            if summary_usage is not None:
                inference.append(summary_usage)
        commerce["timings"] = {
            "stages_seconds": timings.snapshot(),
            "search_stages_seconds": {
                name: round(value, 6) for name, value in search_stages.items()
            },
            "search_calls": search_calls,
            "products_ready_seconds": round(products_ready, 6)
            if products_ready is not None
            else None,
            "before_commit_seconds": round(timings.elapsed(), 6),
        }
        response = {
            "conversation_id": key,
            "reply": commerce["reply"],
            "model": self.settings.model,
            "done_reason": result.get("done_reason", "deterministic"),
            "commerce": commerce,
            "usage": aggregate_usage(inference),
        }
        with timings.measure("commit_seconds"):
            self.state.store.commit_commerce_turn(
                key, message, state, commerce, record["state_version"], identity, response
            )
        for stage, seconds in timings.seconds.items():
            self.state.metrics.increment("turn_" + stage + "_total", seconds)
        self.state.metrics.increment("turn_route_" + route["mode"] + "_total")
        return response

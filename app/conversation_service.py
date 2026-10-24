"""Dispatch owned conversations to chat or commerce processing."""

from collections.abc import Callable

from starlette.datastructures import State

from app.api_models import ChatRequest
from app.chat_service import ChatService
from app.commerce_service import CommerceTurnService
from app.commerce_storefront import TurnHandler
from app.config import Settings
from app.inference_usage import aggregate_usage


class ConversationService:
    def __init__(
        self, state: State, settings: Settings, chat: ChatService, turn_handler: TurnHandler
    ) -> None:
        self.state = state
        self.settings = settings
        self.chat = chat
        self.commerce = CommerceTurnService(state, settings, chat, turn_handler)

    async def process_message(
        self,
        key: str,
        body: ChatRequest,
        user_id: str,
        identity,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        record = self.state.store.check_owner(key, user_id)
        if record["mode"] == "commerce":
            return await self.commerce.process_message(record, body.message, identity, progress)
        history = self.state.store.messages(key, record["summarized_through"], limit=1000)
        messages, summary, boundary = await self.chat.prepare_context(record, history, body.message)
        answer, result = await self.chat.complete(messages)
        response = {
            "conversation_id": key,
            "reply": answer,
            "model": self.settings.model,
            "done_reason": result.get("done_reason"),
            "usage": aggregate_usage([result]),
        }

        self.state.store.commit_turn(
            key, body.message, answer, summary, boundary, identity, response
        )
        return response

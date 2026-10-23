"""HTTP endpoints for conversations."""

from uuid import UUID

from fastapi import HTTPException, Query, Request, Response

from app.api_models import ChatRequest, NewConversation
from app.commerce_models import ShoppingState, preference_view
from app.operations import finish_on_disconnect


def register_routes(app, access, service):
    @app.post("/conversations", status_code=201)
    async def new_conversation(body: NewConversation, request: Request):
        identity, replay = access.claim(request, "create", body)
        if replay is not None:
            return replay
        try:
            return app.state.store.create(body.title, body.mode, access.owner(request), identity)
        finally:
            app.state.store.release_request(identity)

    @app.get("/conversations")
    async def list_conversations(
        request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)
    ):
        return {"conversations": app.state.store.list(limit, offset, access.owner(request))}

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: UUID, request: Request):
        return app.state.store.check_owner(str(conversation_id), access.owner(request))

    @app.get("/conversations/{conversation_id}/preferences")
    async def get_preferences(conversation_id: UUID, request: Request):
        record = app.state.store.check_owner(str(conversation_id), access.owner(request))
        if record["mode"] != "commerce":
            raise HTTPException(
                409, "Create a conversation with mode='commerce' to use shopping preferences"
            )
        return {
            "conversation_id": str(conversation_id),
            "state_version": record["state_version"],
            "preferences": preference_view(ShoppingState.model_validate(record["shopping_state"])),
        }

    @app.get("/conversations/{conversation_id}/messages")
    async def get_messages(
        conversation_id: UUID,
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        key = str(conversation_id)
        app.state.store.check_owner(key, access.owner(request))
        messages = app.state.store.messages(key, after, limit)
        return {"messages": messages, "next_after": messages[-1]["id"] if messages else after}

    @app.post("/conversations/{conversation_id}/messages")
    async def send_message(conversation_id: UUID, body: ChatRequest, request: Request):
        key = str(conversation_id)
        app.state.store.check_owner(key, access.owner(request))
        identity, replay = access.claim(request, "message:" + key, body)
        if replay is not None:
            return replay

        async def work():
            try:
                access.model_rate(request)
                async with app.state.admission.workload(), app.state.admission.conversation(key):
                    return await service.process_message(key, body, access.owner(request), identity)
            finally:
                app.state.store.release_request(identity)

        return await finish_on_disconnect(work())

    @app.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: UUID, request: Request):
        async with app.state.admission.conversation(str(conversation_id)):
            key = str(conversation_id)
            app.state.store.check_owner(key, access.owner(request))
            app.state.store.delete(key)
        return Response(status_code=204)

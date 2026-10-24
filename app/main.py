"""Application composition and single-worker lifecycle."""

import asyncio
import fcntl
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from app import routes_conversations, routes_operations, routes_search
from app.api_access import RequestAccess
from app.api_schema import configure_schema
from app.chat_service import ChatService
from app.commerce_flow import handle_turn
from app.config import ROOT as ROOT
from app.config import Settings as Settings
from app.conversation_service import ConversationService
from app.conversation_store import Store as Store
from app.dependencies import probe_dependencies
from app.operations import (
    Admission,
    Metrics,
    OperationalMiddleware,
    OperationsSettings,
    TokenBuckets,
)
from app.product_search import ProductSearch
from app.providers import llm_client_options, validate_llm
from app.reranker import create_reranker
from app.text_budget import byte_size as byte_size

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    operations: OperationsSettings | None = None,
) -> FastAPI:
    operations = operations or OperationsSettings.from_env()
    settings = settings or Settings()
    validate_llm(settings)
    if settings.max_tokens < 1 or settings.timeout <= 0:
        raise ValueError("Model token limit and timeout must be positive")
    if settings.prompt_budget < 7000 or byte_size(settings.system_prompt) > 1500:
        raise ValueError(
            "Reserve at least 7000 context tokens for prompts; system prompt must fit in 1500 UTF-8 bytes"
        )

    @asynccontextmanager
    async def lifespan(app):
        path = Path(settings.database)
        path = (path if path.is_absolute() else ROOT / path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(str(path) + ".server.lock", "a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            raise RuntimeError(
                "One API worker per conversation database is supported; stop the other worker first"
            ) from None
        try:
            app.state.server_lock = lock_file
            app.state.store = Store(path)
            app.state.metrics = Metrics()
            app.state.metrics.increment(
                "recovered_requests_total", app.state.store.recover_requests()
            )
            app.state.admission = Admission(operations, app.state.metrics)
            app.state.model_buckets = TokenBuckets()
            app.state.reranker = create_reranker()
            app.state.product_search = ProductSearch(reranker=app.state.reranker)
            app.state.product_search.cache.metrics = app.state.metrics
            app.state.probe_lock = asyncio.Lock()
            async with httpx.AsyncClient(
                **llm_client_options(settings),
                timeout=httpx.Timeout(settings.timeout, connect=5),
                transport=transport,
                trust_env=False,
            ) as client:
                app.state.ollama = client
                app.state.llm_provider = settings.provider
                app.state.search_warmup = {"status": "disabled"}
                if os.getenv("SEARCH_WARMUP", "0") == "1":
                    app.state.search_warmup = await asyncio.to_thread(
                        app.state.product_search.warmup
                    )
                monitor = (
                    asyncio.create_task(monitor_loop()) if operations.monitor_interval else None
                )
                try:
                    yield
                finally:
                    if monitor:
                        monitor.cancel()
                        try:
                            await monitor
                        except asyncio.CancelledError:
                            pass
                    cache = getattr(app.state.product_search, "cache", None)
                    if cache is not None:
                        cache.close()
        finally:
            lock_file.close()

    app = FastAPI(title="Local Conversational AI", version="1.0.0", lifespan=lifespan)
    app.add_middleware(OperationalMiddleware, settings=operations, state=app.state)
    access = RequestAccess(app.state, operations)
    chat = ChatService(app.state, settings)
    service = ConversationService(app.state, settings, chat, handle_turn)

    async def refresh_status():
        async with app.state.probe_lock:
            await probe_dependencies(app.state, settings.model)
        return app.state.metrics.evaluate(operations, app.state.admission)

    async def monitor_loop():
        while True:
            try:
                await refresh_status()
            except Exception:
                logger.error("Operational monitor failed", exc_info=False)
                app.state.metrics.increment("monitor_failures_total")
            await asyncio.sleep(operations.monitor_interval)

    routes_conversations.register_routes(app, access, service)
    routes_search.register_routes(app, access)
    routes_operations.register_routes(app, settings, operations, refresh_status)
    configure_schema(app, operations)
    return app


app = create_app()

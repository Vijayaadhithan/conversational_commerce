"""HTTP endpoints for operations."""

from fastapi import HTTPException, Response
from fastapi.responses import JSONResponse

from app.providers import probe_llm


def register_routes(app, settings, operations, refresh_status):
    @app.get("/livez")
    async def livez():
        return {"status": "alive"}

    @app.get("/ready")
    async def ready():
        status = await refresh_status()
        required = ("sqlite", "models", "postgres", "reranker")
        available = all(status["dependencies"][name]["status"] == "ready" for name in required)
        return JSONResponse(
            {"status": "ready" if available else "unavailable"},
            status_code=200 if available else 503,
        )

    @app.get("/ops/status")
    async def operational_status():
        return await refresh_status()

    @app.get("/ops/metrics")
    async def operational_metrics():
        app.state.metrics.evaluate(operations, app.state.admission)
        return Response(
            app.state.metrics.prometheus(app.state.admission),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/health")
    async def health():
        try:
            await probe_llm(app.state.ollama, settings.provider, settings.model)
        except Exception:
            raise HTTPException(503, "Configured model unavailable") from None
        return {
            "status": "ok",
            "model": settings.model,
            "provider": settings.provider,
            "search_warmup": app.state.search_warmup,
        }

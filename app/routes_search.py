"""HTTP endpoints for search."""

import asyncio

import psycopg
from fastapi import HTTPException, Request

from app.api_models import RerankRequest
from app.catalog import CatalogError
from app.operations import finish_on_disconnect
from app.search_models import ProductSearchRequest


def register_routes(app, access):
    @app.post("/products/search")
    async def products_search(body: ProductSearchRequest, request: Request):
        access.model_rate(request)
        return await finish_on_disconnect(search_work(body))

    async def search_work(body):
        async with app.state.admission.workload():
            try:
                return await asyncio.to_thread(app.state.product_search.search, body)
            except CatalogError as exc:
                raise HTTPException(503, str(exc)) from exc
            except (psycopg.Error, OSError) as exc:
                raise HTTPException(
                    503,
                    "Product search unavailable. Check PostgreSQL and initialize/build the search catalog.",
                ) from exc

    @app.post("/rerank")
    async def rerank(body: RerankRequest, request: Request):
        access.model_rate(request)
        return await finish_on_disconnect(rerank_work(body))

    async def rerank_work(body):
        async with app.state.admission.workload():
            try:
                return await asyncio.to_thread(
                    app.state.reranker.rank, body.query, body.documents, body.top_k
                )
            except (RuntimeError, OSError) as exc:
                detail = (
                    "Local reranker is missing. See README.md for its download command."
                    if str(exc).startswith("Local reranker is missing.")
                    else "Reranker unavailable; check provider configuration and service availability"
                )
                raise HTTPException(503, detail) from None

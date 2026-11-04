"""HTTP endpoints for search."""

import asyncio

import psycopg
from fastapi import HTTPException, Request

from app.api_models import RerankRequest
from app.catalog import CatalogError
from app.catalog_summary import catalog_summary
from app.operations import finish_on_disconnect
from app.search_models import ProductSearchRequest


def register_routes(app, access):
    @app.get("/catalog/summary")
    async def summary():
        try:
            async with app.state.summary_lock:
                return await asyncio.to_thread(
                    catalog_summary, app.state.product_search.database_url
                )
        except (CatalogError, psycopg.Error, OSError):
            raise HTTPException(503, "Catalog overview unavailable") from None

    @app.post("/products/search")
    async def products_search(body: ProductSearchRequest, request: Request):
        access.model_rate(request)
        return await finish_on_disconnect(
            app.state.runtime.run(
                lambda: search_work(body, access.owner(request)), access.owner(request)
            )
        )

    async def search_work(body, owner):
        async with app.state.admission.workload(owner):
            try:
                return await asyncio.to_thread(app.state.product_search.search, body)
            except CatalogError:
                raise HTTPException(503, "Product search unavailable; retry later") from None
            except (psycopg.Error, OSError) as exc:
                raise HTTPException(
                    503,
                    "Product search unavailable. Check PostgreSQL and initialize/build the search catalog.",
                ) from exc

    @app.post("/rerank")
    async def rerank(body: RerankRequest, request: Request):
        access.model_rate(request)
        return await finish_on_disconnect(
            app.state.runtime.run(
                lambda: rerank_work(body, access.owner(request)), access.owner(request)
            )
        )

    async def rerank_work(body, owner):
        async with app.state.admission.workload(owner):
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

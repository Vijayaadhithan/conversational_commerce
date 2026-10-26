"""Scoped hybrid retrieval with PostgreSQL hydration and shared ranking/evidence."""

import os
import re
import time

from app.catalog import CatalogError, connect_database
from app.embedding_text import contract
from app.embeddings import EmbeddingSettings, create_embedder, query_vector
from app.listing_explanations import concern_evidence
from app.listing_quality import listing_quality, prioritize_listing_evidence
from app.product_reranking import rerank_candidates as apply_reranking
from app.product_reranking import rerank_document
from app.qdrant_client import QdrantClient, QdrantSettings
from app.qdrant_index import pin_qdrant_index
from app.reranker import create_reranker
from app.search_cache import SearchCache
from app.search_filters import filter_sql, product_goal_sql
from app.search_models import AttributeName as AttributeName
from app.search_models import AttributeValue as AttributeValue
from app.search_models import ProductFilters as ProductFilters
from app.search_models import ProductID as ProductID
from app.search_models import ProductSearchRequest as ProductSearchRequest
from app.search_normalization import normalize_search
from app.storefronts import category_allowed


def pin_search_build(db):
    row = db.execute("""SELECT b.build_id,b.import_id,b.config,b.expected_count,i.run_id
        FROM commerce.search_active a JOIN commerce.search_builds b USING(build_id)
        JOIN commerce.catalog_active c ON c.import_id=b.import_id
        JOIN commerce.catalog_imports i ON i.import_id=b.import_id
        WHERE a.singleton AND c.singleton AND b.status='ready'""").fetchone()
    if not row:
        raise CatalogError(
            "No complete search build for the active catalog; run scripts/build_search_index.py"
        )
    if row[2]["input_contract"] != contract():
        raise CatalogError("Search input contract changed; rebuild the search catalog")
    return {
        "build_id": row[0],
        "import_id": row[1],
        "config": row[2],
        "catalog_products": row[3],
        "run_id": row[4],
    }


def reciprocal_rank_fusion(keyword, semantic):
    result = {}
    for kind, rows in (("keyword", keyword), ("vector", semantic)):
        for rank, (product_id, score) in enumerate(rows, 1):
            entry = result.setdefault(
                product_id,
                {
                    "rrf_score": 0.0,
                    "keyword_rank": None,
                    "vector_rank": None,
                    "cosine_similarity": None,
                },
            )
            entry["rrf_score"] += 1 / (60 + rank)
            entry[kind + "_rank"] = rank
            if kind == "vector":
                entry["cosine_similarity"] = 1 - score
    return sorted(result.items(), key=lambda item: (-item[1]["rrf_score"], item[0]))


def listing_evidence(record, query):
    terms = set(re.findall(r"[^\W_]{3,}", query.lower())) - {
        "the",
        "for",
        "with",
        "and",
        "that",
        "under",
        "want",
    }
    evidence = []
    fields = [("/title", record["title"])]
    for name in ("color", "material", "product_type", "department"):
        claim = record.get("attributes", {}).get(name)
        if claim and isinstance(claim["value"], str):
            fields.append((claim["field"], claim["value"]))
    for name in ("features", "description"):
        fields.extend(
            zip(record["content_source_fields"][name], record["content"][name], strict=True)
        )
    for field, text in fields:
        matched = sorted(terms & set(re.findall(r"[^\W_]{3,}", text.lower())))
        if matched:
            # Return an actual contiguous listing excerpt around the first matched term.
            lower = text.lower()
            position = min(lower.find(term) for term in matched)
            start = max(0, position - 60)
            excerpt = text[start : start + 300]
            excerpt_terms = set(re.findall(r"[^\W_]{3,}", excerpt.lower()))
            evidence.append(
                {
                    "field": field,
                    "text": excerpt,
                    "matching_terms": sorted(terms & excerpt_terms),
                    "status": "listing_excerpt",
                }
            )
        if len(evidence) == 2:
            break
    return evidence


def search_catalog(
    db,
    request,
    embedder,
    pinned=None,
    *,
    candidate_limit=None,
    candidate_texts=None,
    qdrant=None,
    collection=None,
):
    if qdrant is None:
        with QdrantClient(QdrantSettings.from_env()) as client:
            return search_catalog(
                db,
                request,
                embedder,
                pinned,
                candidate_limit=candidate_limit,
                candidate_texts=candidate_texts,
                qdrant=client,
                collection=collection,
            )
    pinned = pinned or pin_search_build(db)
    config = pinned["config"]
    if embedder.settings.model != config["model"]:
        raise CatalogError(
            "Configured embedding model differs from the search build; rebuild first"
        )
    suffix, params = filter_sql(request.filters)
    goal_sql, goal_params = product_goal_sql(request.product_goal)
    suffix += goal_sql
    params.extend(goal_params)
    eligible_sql = (
        "SELECT p.product_id FROM commerce.catalog_products p WHERE p.import_id=%s" + suffix
    )
    collection = collection or pin_qdrant_index(db, pinned, qdrant)
    eligible_ids = [
        row[0]
        for row in db.execute(
            eligible_sql + " ORDER BY p.product_id LIMIT %s",
            [pinned["import_id"], *params, qdrant.settings.max_eligible + 1],
        ).fetchall()
    ]
    if len(eligible_ids) > qdrant.settings.max_eligible:
        raise CatalogError("Qdrant eligibility limit exceeded; narrow the search filters")
    eligible_count = len(eligible_ids)
    base = {
        "query": request.query,
        "run_id": pinned["run_id"],
        "build_id": pinned["build_id"],
        "model": config["model"],
        "model_digest": config["model_digest"],
        "filters_applied": request.filters.model_dump(mode="json", exclude_none=True),
        "eligible_products": eligible_count,
        "method": (
            "qdrant_bm25+" + ("exact_cosine" if qdrant.settings.exact else "hnsw_cosine") + "+rrf"
        ),
        "constraints_source": "structured_filters",
        "product_goal": request.product_goal,
        "price_context": "historical USD",
        "results": [],
    }
    if not eligible_count:
        return base
    embedder.verify_identity(config["model_digest"])
    retrieval_query = normalize_search(request.query)
    vector = query_vector(embedder, retrieval_query)
    keyword, semantic = qdrant.branches(
        collection,
        pinned,
        request.model_copy(update={"query": retrieval_query}),
        vector,
        eligible_ids,
    )
    base["retrieval_collection"] = collection
    fused = reciprocal_rank_fusion(keyword, semantic)[: candidate_limit or request.limit]
    records = dict(
        db.execute(
            "SELECT product_id,document FROM commerce.catalog_products WHERE import_id=%s AND product_id=ANY(%s)",
            (pinned["import_id"], [item[0] for item in fused]),
        ).fetchall()
    )
    for product_id, ranking in fused:
        record = records[product_id]
        if not category_allowed(record.get("category"), request.filters.storefront_id):
            raise CatalogError("Catalog category does not match the requested storefront")
        evidence = concern_evidence(record, request.concerns)
        if candidate_texts is not None:
            candidate_texts[product_id] = rerank_document(record)
        base["results"].append(
            {
                "product_id": product_id,
                "title": record["title"],
                "category": record["category"],
                "brand": record["brand"],
                "price": record["price"],
                "attributes": record["attributes"],
                "availability": record["availability"],
                "image_urls": record["image_urls"][:3],
                "selected_observation": record["selected_observation"],
                "ranking": ranking,
                "listing_evidence": listing_evidence(record, request.query),
                "concern_evidence": evidence,
                "listing_quality": listing_quality(record, request, evidence),
                "variant_metadata": {
                    "selected": {
                        name: record["attributes"][name]
                        for name in ("size", "color", "scent", "style", "volume")
                        if record["attributes"].get(name)
                    },
                    "listed_options": {
                        name: record["attributes"][name]
                        for name in ("size_options", "color_options")
                        if record["attributes"].get(name)
                    },
                    "purchasable_variants": None,
                    "availability": None,
                },
            }
        )
    return base


class ProductSearch:
    def __init__(
        self, database_url=None, settings=None, reranker=None, rerank_candidates=None, cache=None
    ):
        self.qdrant_settings = QdrantSettings.from_env()
        self.database_url = database_url or os.getenv(
            "CATALOG_DATABASE_URL", "postgresql:///conversational_commerce"
        )
        self.settings = settings or EmbeddingSettings.from_env()
        self.reranker = reranker if reranker is not None else create_reranker()
        self.cache = cache if cache is not None else SearchCache.from_env()
        self.rerank_candidates = (
            rerank_candidates
            if rerank_candidates is not None
            else int(os.getenv("PRODUCT_RERANK_CANDIDATES", "20"))
        )
        if not 20 <= self.rerank_candidates <= 50:
            raise CatalogError("PRODUCT_RERANK_CANDIDATES must be between 20 and 50")

    def search(self, request):
        started = time.perf_counter()
        texts = {} if request.rerank else None
        with (
            connect_database(self.database_url) as db,
            QdrantClient(self.qdrant_settings) as qdrant,
        ):
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            pinned = pin_search_build(db)
            collection = pin_qdrant_index(db, pinned, qdrant)
            with create_embedder(self.settings) as embedder:
                if self.settings.model != pinned["config"]["model"]:
                    raise CatalogError(
                        "Configured embedding model differs from the search build; rebuild first"
                    )
                embedder.verify_identity(pinned["config"]["model_digest"])
                runtime = {
                    "search_backend": "qdrant",
                    "qdrant_collection": collection,
                    "qdrant_exact": self.qdrant_settings.exact,
                    "rerank_provider": os.getenv("RERANK_PROVIDER", "local"),
                    "rerank_model": os.getenv("VOYAGE_RERANK_MODEL", "rerank-2.5"),
                    "candidates": self.rerank_candidates,
                    "rerank_batch": getattr(self.reranker, "batch_size", None),
                    "rerank_length": getattr(self.reranker, "max_length", None),
                    "rerank_device": os.getenv("RERANK_DEVICE", "auto"),
                    "rerank_path": os.getenv("RERANK_MODEL_PATH", "default"),
                }
                key = self.cache.key(request, pinned, runtime)
                cached, cache_status = self.cache.get(key)
                if (
                    cached is not None
                    and cached.get("build_id") == pinned["build_id"]
                    and cached.get("query") == request.query
                    and cached.get("filters_applied")
                    == request.filters.model_dump(mode="json", exclude_none=True)
                    and len(cached["results"]) <= request.limit
                    and all(
                        all(
                            name in row
                            for name in ("product_id", "title", "price", "attributes", "category")
                        )
                        and category_allowed(row.get("category"), request.filters.storefront_id)
                        for row in cached["results"]
                    )
                ):
                    cached["cache"] = {"status": "hit"}
                    cached["timings"] = {"total_seconds": time.perf_counter() - started}
                    return cached
                if cached is not None:
                    cache_status = "invalid"
                retrieved = time.perf_counter()
                result = search_catalog(
                    db,
                    request,
                    embedder,
                    pinned,
                    candidate_limit=max(self.rerank_candidates, request.limit)
                    if request.rerank or request.product_goal or request.concerns
                    else request.limit,
                    candidate_texts=texts,
                    qdrant=qdrant,
                    collection=collection,
                )
        # All product data was hydrated from one pinned snapshot; release PostgreSQL
        # before potentially slow cold model loading/scoring.
        retrieval_seconds = time.perf_counter() - retrieved
        if request.rerank:
            result = apply_reranking(
                result,
                texts,
                request.model_copy(update={"query": normalize_search(request.query)}),
                self.reranker,
                defer_limit=True,
            )
        else:
            result["reranking"] = {
                "requested": False,
                "applied": False,
                "reason": "disabled",
                "candidate_count": 0,
            }
        result = prioritize_listing_evidence(result, request)
        if any(
            not category_allowed(row.get("category"), request.filters.storefront_id)
            for row in result["results"]
        ):
            raise CatalogError("Search results do not match the requested storefront")
        self.cache.put(key, result)
        result["cache"] = {"status": cache_status}
        result["timings"] = {
            "retrieval_seconds": retrieval_seconds,
            "total_seconds": time.perf_counter() - started,
        }
        return result

    def warmup(self):
        started = time.perf_counter()
        with connect_database(self.database_url) as db, create_embedder(self.settings) as embedder:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            pinned = pin_search_build(db)
            with QdrantClient(self.qdrant_settings) as client:
                pin_qdrant_index(db, pinned, client)
            if self.settings.model != pinned["config"]["model"]:
                raise CatalogError(
                    "Configured embedding model differs from the search build; rebuild first"
                )
            embedder.verify_identity(pinned["config"]["model_digest"])
            query_vector(embedder, "moisturizer")
        self.reranker.rank("moisturizer", ["A moisturizer listing."], 1, preserve_query=True)
        return {
            "status": "ready",
            "seconds": time.perf_counter() - started,
            "build_id": pinned["build_id"],
        }

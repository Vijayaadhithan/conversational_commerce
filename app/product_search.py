"""Exact filtered vector retrieval + PostgreSQL full text, fused by rank."""

from decimal import Decimal
import os
import re
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from app.catalog import CatalogError, connect_database
from app.embedding_text import QUERY_PREFIX, contract
from app.embeddings import EmbeddingSettings, OllamaEmbedder, vector_literal
from app.product_reranking import rerank_candidates as apply_reranking, rerank_document
from app.reranker import LocalReranker
from app.listing_explanations import concern_evidence
from app.listing_quality import listing_quality, prioritize_listing_evidence
from app.search_cache import SearchCache

AttributeName = Literal["color", "size", "material", "department", "age_range", "skin_type",
    "hair_type", "ingredients", "active_ingredients", "benefits", "material_type_free",
    "scent", "item_form", "unit_count", "fragrance_free", "product_type", "volume", "style"]
AttributeValue = StrictBool | Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ProductID = Annotated[str, StringConstraints(pattern=r"^amazon:[A-Z0-9]{10}$")]


class ProductFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    domain: Literal["beauty", "fashion"] | None = None
    family: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{1,40}$")
    brand: str | None = Field(default=None, min_length=1, max_length=160)
    min_price: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    max_price: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=2)
    attributes: dict[AttributeName, AttributeValue] = Field(default_factory=dict)
    exclude_product_ids: list[ProductID] = Field(default_factory=list, max_length=100)
    exclude_brands: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]] = Field(default_factory=list, max_length=10)

    @field_validator("brand", "family")
    @classmethod
    def valid_text(cls, value):
        if value is not None:
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise ValueError("Filter text must be valid Unicode") from exc
            if "\x00" in value:
                raise ValueError("Filter text cannot contain NUL")
        return value

    @model_validator(mode="after")
    def check_constraints(self):
        for brand in self.exclude_brands:
            self.valid_text(brand)
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price must not exceed max_price")
        for key, value in self.attributes.items():
            if (key == "fragrance_free") != isinstance(value, bool):
                raise ValueError("fragrance_free requires a boolean; other attribute filters require text")
            if isinstance(value, str):
                self.valid_text(value)
        return self


class ProductSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000,
                       description="Relevance text; mandatory constraints must also be provided in filters. Maximum 1,000 UTF-8 bytes.")
    filters: ProductFilters = Field(default_factory=ProductFilters,
                                   description="Mandatory constraints applied to both retrieval paths; never relaxed.")
    limit: int = Field(default=5, ge=1, le=20)
    rerank: StrictBool = Field(default=True, description="Rerank eligible candidates with the installed local model. False explicitly returns the RRF baseline.")
    product_goal: str = Field(default="", max_length=1200, description="Explicit product type from the user; source wording gates retrieval before ranking.")
    concerns: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=20, description="Soft concerns for separate source-backed explanations; not mandatory filters.")

    @field_validator("query")
    @classmethod
    def validate_query(cls, value):
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("Query must be valid Unicode") from exc
        if not value.strip() or size > 1000 or "\x00" in value:
            raise ValueError("Query must contain text and fit within 1,000 UTF-8 bytes without NUL")
        return value


def filter_sql(filters):
    clauses, values = [], []
    for field in ("domain", "family", "brand"):
        value = getattr(filters, field)
        if value is not None:
            clauses.append("lower(p.brand)=lower(%s)" if field == "brand" else f"p.{field}=%s")
            values.append(value)
    for field, op in (("min_price", ">="), ("max_price", "<=")):
        value = getattr(filters, field)
        if value is not None:
            clauses.append(f"p.historical_price {op} %s")
            values.append(value)
    for key, value in sorted(filters.attributes.items()):
        if key == "material":
            clauses.append("NOT coalesce((p.document #> '{quality,warnings}') ? %s, false)")
            values.append("conflicting_material_composition")
        if isinstance(value, bool):
            clauses.append("p.attributes #> ARRAY[%s,'value'] = %s::jsonb")
            values.extend((key, "true" if value else "false"))
        elif key == "department" and re.sub(r"[^a-z]", "", str(value).lower()) in {"men", "mens", "women", "womens", "boys", "girls"}:
            canonical = re.sub(r"[^a-z]", "", str(value).lower()).removesuffix("s")
            clauses.append("regexp_replace(lower(p.attributes #>> ARRAY[%s,'value']), '[^a-z]', '', 'g') = ANY(%s)")
            values.extend((key, [canonical, canonical + "s"]))
        else:
            clauses.append("lower(p.attributes #>> ARRAY[%s,'value'])=lower(%s)")
            values.extend((key, value))
    if filters.exclude_product_ids:
        clauses.append("NOT (p.product_id=ANY(%s))")
        values.append(filters.exclude_product_ids)
    if filters.exclude_brands:
        clauses.append("p.brand IS NOT NULL AND NOT (lower(p.brand)=ANY(%s))")
        values.append([brand.lower() for brand in filters.exclude_brands])
    return (" AND " + " AND ".join(clauses)) if clauses else "", values


def product_goal_sql(goal):
    """Conservative literal product identity gates; unknown types retain normal retrieval.

    Title/type wording is required, with description/features allowed for facial
    moisturizer aliases. This is textual listing support, not suitability inference.
    """
    rules = [
        (r"\brazor blades?\b", r"\m(blades?|cartridges?|refills?)\M"),
        (r"\blipsticks?\b", r"\mlipsticks?\M"),
        (r"\b(?:handbag|bag|crossbody|purse)s?\b", r"\m(handbags?|bags?|crossbody|purses?)\M"),
        (r"\bshampoo\b", r"\mshampoos?\M"),
        (r"\bconditioner\b", r"\mconditioners?\M"),
        (r"\bmoisturi\w*|\bface cream\b", r"\m(moisturi[sz][a-z]*|emulsions?|creams?)\M"),
        (r"\bcleanser\b", r"\mcleansers?\M"),
        (r"\bshirts?\b", r"\mshirts?\M"),
        (r"\b(?:shoes?|sneakers?)\b", r"\m(shoes?|sneakers?)\M"),
        (r"\bnecklaces?\b", r"\mnecklaces?\M"),
        (r"\bserum\b", r"\mserums?\M"),
        (r"\bsunscreen\b", r"\msunscreens?\M"),
    ]
    identity = "concat_ws(' ', p.document->>'title', p.attributes #>> '{product_type,value}')"
    clauses, values = [], []
    for trigger, pattern in rules:
        if re.search(trigger, goal, re.I):
            clauses.append(identity + " ~* %s")
            values.append(pattern)
            break
    full_text = "concat_ws(' ', p.document->>'title', p.document #>> '{content,features}', p.document #>> '{content,description}')"
    # Product names can describe a container/accessory or a different use entirely.
    if re.search(r"\blipsticks?\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(empty|containers?|holders?|organizers?|brooch|lapel|keychain|keyring)\M")
    if re.search(r"\brazor blades?\b", goal, re.I):
        clauses.append(full_text + " !~* %s")
        values.append(r"\m(scrapers?|scraping|paint|decals?|utility knife|box cutter)\M")
        clauses.append(full_text + " ~* %s")
        values.append(r"\m(shav[a-z]*|beards?|barbers?|safety razors?)\M")
        clauses.append("((p.document->>'title') !~* %s OR (p.document->>'title') ~* %s)")
        values.extend([r"\m(disposable|shavers?)\M", r"\m(refills?|replacement|cartridges?)\M"])
    if re.search(r"\bmoisturi\w*|\bface cream\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(masks?|peels?|cleansers?)\M")
    if re.search(r"\b(?:shoes?|sneakers?)\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(charms?|keychain|key chain|zipper pull|cleaning|cleaner|stain remover|insoles?|laces?|socks?|shorts?|pants?|leggings?)\M")
        if re.search(r"\brunning\b", goal, re.I):
            clauses.append("(p.document->>'title') ~* %s")
            values.append(r"\mrunning\M")
    if re.search(r"\b(?:handbag|purse|crossbody)s?\b", goal, re.I) and not re.search(r"\b(?:cosmetic|makeup|toiletry)\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(organizers?|inserts?|liners?|cosmetic|makeup|toiletry|keychain|charms?)\M")
    if re.search(r"\bcrossbody\b", goal, re.I):
        clauses.append("(p.document->>'title') ~* %s")
        values.append(r"\mcross[ -]?body\M")
    if re.search(r"\bnecklaces?\b", goal, re.I) and not re.search(r"\b(?:holder|organizer|display|extender|clasp)\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(holders?|organizers?|display|extenders?|clasps?|jewelry box)\M")
    # A selected shade or title must support an explicitly named color. Generic
    # marketing about other shades elsewhere in a listing is not selected-variant evidence.
    colors = r"red|pink|blue|black|white|green|purple|orange|yellow|brown|silver|gold"
    requested_colors = set(re.findall(r"\b(?:" + colors + r")\b", goal.lower()))
    if clauses and len(requested_colors) == 1:
        color = requested_colors.pop()
        clauses.append("concat_ws(' ', p.document->>'title', p.attributes #>> '{color,value}', p.attributes #>> '{material,value}') ~* %s")
        values.append(r"\m" + color + r"\M")
        clauses.append("(coalesce(p.attributes #>> '{color,value}', '') !~* %s OR (p.attributes #>> '{color,value}') ~* %s)")
        values.extend([r"\m(" + colors + r")\M", r"\m" + color + r"\M"])
    if re.search(r"\bcotton\b", goal, re.I):
        clauses.append(full_text + " ~* %s AND " + full_text + " !~* %s")
        values.extend([r"\mcotton\M", r"\m(no|without|not) cotton\M"])
    if re.search(r"\bpockets?\b", goal, re.I):
        clauses.append(full_text + " ~* %s AND " + full_text + " !~* %s")
        values.extend([r"\mpockets?\M", r"\m(no|without|fake|faux) (chest |side )?pockets?\M"])
    if clauses and re.search(r"\bface|\bfacial", goal, re.I):
        clauses.append("concat_ws(' ', p.document->>'title', p.document #>> '{content,features}', p.document #>> '{content,description}') ~* %s")
        values.append(r"\m(face|facial)\M")
    return (" AND " + " AND ".join(clauses)) if clauses else "", values


def pin_search_build(db):
    row = db.execute("""SELECT b.build_id,b.import_id,b.config,b.expected_count,i.run_id
        FROM commerce.search_active a JOIN commerce.search_builds b USING(build_id)
        JOIN commerce.catalog_active c ON c.import_id=b.import_id
        JOIN commerce.catalog_imports i ON i.import_id=b.import_id
        WHERE a.singleton AND c.singleton AND b.status='ready'""").fetchone()
    if not row:
        raise CatalogError("No complete search build for the active catalog; run scripts/build_search_index.py")
    if row[2]["input_contract"] != contract():
        raise CatalogError("Search input contract changed; rebuild the search catalog")
    return {"build_id": row[0], "import_id": row[1], "config": row[2], "catalog_products": row[3], "run_id": row[4]}


def reciprocal_rank_fusion(keyword, semantic):
    result = {}
    for kind, rows in (("keyword", keyword), ("vector", semantic)):
        for rank, (product_id, score) in enumerate(rows, 1):
            entry = result.setdefault(product_id, {"rrf_score": 0.0, "keyword_rank": None,
                "vector_rank": None, "cosine_similarity": None})
            entry["rrf_score"] += 1 / (60 + rank)
            entry[kind + "_rank"] = rank
            if kind == "vector": entry["cosine_similarity"] = 1 - score
    return sorted(result.items(), key=lambda item: (-item[1]["rrf_score"], item[0]))


def listing_evidence(record, query):
    terms = set(re.findall(r"[^\W_]{3,}", query.lower())) - {"the", "for", "with", "and", "that", "under", "want"}
    evidence = []
    fields = [("/title", record["title"])]
    for name in ("color", "material", "product_type", "department"):
        claim = record.get("attributes", {}).get(name)
        if claim and isinstance(claim["value"], str):
            fields.append((claim["field"], claim["value"]))
    for name in ("features", "description"):
        fields.extend(zip(record["content_source_fields"][name], record["content"][name], strict=True))
    for field, text in fields:
        matched = sorted(terms & set(re.findall(r"[^\W_]{3,}", text.lower())))
        if matched:
            # Return an actual contiguous listing excerpt around the first matched term.
            lower = text.lower()
            position = min(lower.find(term) for term in matched)
            start = max(0, position - 60)
            excerpt = text[start:start + 300]
            excerpt_terms = set(re.findall(r"[^\W_]{3,}", excerpt.lower()))
            evidence.append({"field": field, "text": excerpt,
                             "matching_terms": sorted(terms & excerpt_terms), "status": "listing_excerpt"})
        if len(evidence) == 2: break
    return evidence


def search_catalog(db, request, embedder, pinned=None, *, candidate_limit=None, candidate_texts=None):
    pinned = pinned or pin_search_build(db)
    config = pinned["config"]
    if embedder.settings.model != config["model"]:
        raise CatalogError("Configured embedding model differs from the search build; rebuild first")
    suffix, params = filter_sql(request.filters)
    goal_sql, goal_params = product_goal_sql(request.product_goal)
    suffix += goal_sql
    params.extend(goal_params)
    eligible_sql = "SELECT p.product_id FROM commerce.catalog_products p WHERE p.import_id=%s" + suffix
    eligible_count = db.execute("SELECT count(*) FROM (" + eligible_sql + ") eligible", [pinned["import_id"], *params]).fetchone()[0]
    base = {"query": request.query, "run_id": pinned["run_id"], "build_id": pinned["build_id"],
        "model": config["model"], "model_digest": config["model_digest"],
        "filters_applied": request.filters.model_dump(mode="json", exclude_none=True),
        "eligible_products": eligible_count, "method": "postgres_fts+exact_cosine+rrf",
        "constraints_source": "structured_filters", "product_goal": request.product_goal,
        "price_context": "historical USD", "results": []}
    if not eligible_count:
        return base
    embedder.verify_identity(config["model_digest"])
    vector = embedder.embed([QUERY_PREFIX + request.query])[0]
    # Materialize only IDs, not full records/vectors; both candidate branches obey the same filters.
    rows = db.execute("""WITH eligible AS MATERIALIZED (""" + eligible_sql + """),
        keyword AS (
            SELECT d.product_id,ts_rank_cd(d.lexical,plainto_tsquery('english',%s))::float8 AS score
            FROM commerce.search_documents d JOIN eligible e USING(product_id)
            WHERE d.build_id=%s AND d.lexical @@ plainto_tsquery('english',%s)
            ORDER BY score DESC,d.product_id LIMIT 50),
        semantic AS (
            SELECT d.product_id,(d.embedding <=> %s::public.vector) AS score
            FROM commerce.search_documents d JOIN eligible e USING(product_id)
            WHERE d.build_id=%s ORDER BY score,d.product_id LIMIT 50)
        SELECT 'keyword',product_id,score FROM keyword UNION ALL SELECT 'vector',product_id,score FROM semantic""",
        [pinned["import_id"], *params, request.query, pinned["build_id"], request.query,
         vector_literal(vector), pinned["build_id"]]).fetchall()
    keyword = sorted([(key, score) for kind, key, score in rows if kind == "keyword"], key=lambda item: (-item[1], item[0]))
    semantic = sorted([(key, score) for kind, key, score in rows if kind == "vector"], key=lambda item: (item[1], item[0]))
    fused = reciprocal_rank_fusion(keyword, semantic)[:candidate_limit or request.limit]
    records = dict(db.execute("SELECT product_id,document FROM commerce.catalog_products WHERE import_id=%s AND product_id=ANY(%s)",
                              (pinned["import_id"], [item[0] for item in fused])).fetchall())
    for product_id, ranking in fused:
        record = records[product_id]
        evidence = concern_evidence(record, request.concerns)
        if candidate_texts is not None:
            candidate_texts[product_id] = rerank_document(record)
        base["results"].append({"product_id": product_id, "title": record["title"],
            "category": record["category"], "brand": record["brand"], "price": record["price"],
            "attributes": record["attributes"], "availability": record["availability"],
            "image_urls": record["image_urls"][:3], "selected_observation": record["selected_observation"],
            "ranking": ranking, "listing_evidence": listing_evidence(record, request.query),
            "concern_evidence": evidence, "listing_quality": listing_quality(record, request, evidence),
            "variant_metadata": {
                "selected": {name: record["attributes"][name] for name in ("size", "color", "scent", "style", "volume") if record["attributes"].get(name)},
                "listed_options": {name: record["attributes"][name] for name in ("size_options", "color_options") if record["attributes"].get(name)},
                "purchasable_variants": None, "availability": None}})
    return base


class ProductSearch:
    def __init__(self, database_url=None, settings=None, reranker=None, rerank_candidates=None, cache=None):
        self.database_url = database_url or os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
        self.settings = settings or EmbeddingSettings.from_env()
        self.reranker = reranker if reranker is not None else LocalReranker()
        self.cache = cache if cache is not None else SearchCache.from_env()
        self.rerank_candidates = rerank_candidates if rerank_candidates is not None else int(os.getenv("PRODUCT_RERANK_CANDIDATES", "20"))
        if not 20 <= self.rerank_candidates <= 50:
            raise CatalogError("PRODUCT_RERANK_CANDIDATES must be between 20 and 50")

    def search(self, request):
        started = time.perf_counter()
        texts = {} if request.rerank else None
        with connect_database(self.database_url) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            pinned = pin_search_build(db)
            with OllamaEmbedder(self.settings) as embedder:
                if self.settings.model != pinned["config"]["model"]:
                    raise CatalogError("Configured embedding model differs from the search build; rebuild first")
                embedder.verify_identity(pinned["config"]["model_digest"])
                runtime = {"candidates": self.rerank_candidates,
                    "rerank_batch": getattr(self.reranker, "batch_size", None),
                    "rerank_length": getattr(self.reranker, "max_length", None),
                    "rerank_device": os.getenv("RERANK_DEVICE", "auto"),
                    "rerank_path": os.getenv("RERANK_MODEL_PATH", "default")}
                key = self.cache.key(request, pinned, runtime)
                cached, cache_status = self.cache.get(key)
                if (cached is not None and cached.get("build_id") == pinned["build_id"] and cached.get("query") == request.query
                        and cached.get("filters_applied") == request.filters.model_dump(mode="json", exclude_none=True)
                        and len(cached["results"]) <= request.limit
                        and all(all(name in row for name in ("product_id", "title", "price", "attributes", "category")) for row in cached["results"])):
                    cached["cache"] = {"status": "hit"}
                    cached["timings"] = {"total_seconds": time.perf_counter() - started}
                    return cached
                if cached is not None: cache_status = "invalid"
                retrieved = time.perf_counter()
                result = search_catalog(db, request, embedder, pinned,
                    candidate_limit=max(self.rerank_candidates, request.limit) if request.rerank or request.product_goal or request.concerns else request.limit,
                    candidate_texts=texts)
        # All product data was hydrated from one pinned snapshot; release PostgreSQL
        # before potentially slow cold model loading/scoring.
        retrieval_seconds = time.perf_counter() - retrieved
        if request.rerank:
            result = apply_reranking(result, texts, request, self.reranker, defer_limit=True)
        else:
            result["reranking"] = {"requested": False, "applied": False, "reason": "disabled", "candidate_count": 0}
        result = prioritize_listing_evidence(result, request)
        self.cache.put(key, result)
        result["cache"] = {"status": cache_status}
        result["timings"] = {"retrieval_seconds": retrieval_seconds, "total_seconds": time.perf_counter() - started}
        return result

    def warmup(self):
        started = time.perf_counter()
        with connect_database(self.database_url) as db, OllamaEmbedder(self.settings) as embedder:
            pinned = pin_search_build(db)
            if self.settings.model != pinned["config"]["model"]:
                raise CatalogError("Configured embedding model differs from the search build; rebuild first")
            embedder.verify_identity(pinned["config"]["model_digest"])
            embedder.embed([QUERY_PREFIX + "moisturizer"])
        self.reranker.rank("moisturizer", ["A moisturizer listing."], 1, preserve_query=True)
        return {"status": "ready", "seconds": time.perf_counter() - started, "build_id": pinned["build_id"]}

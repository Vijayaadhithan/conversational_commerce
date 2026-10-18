"""Exact filtered vector retrieval + PostgreSQL full text, fused by rank."""

from decimal import Decimal
import os
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from app.catalog import CatalogError, connect_database
from app.embedding_text import QUERY_PREFIX, contract
from app.embeddings import EmbeddingSettings, OllamaEmbedder, vector_literal

AttributeName = Literal["color", "size", "material", "department", "age_range", "skin_type",
    "hair_type", "ingredients", "active_ingredients", "benefits", "material_type_free",
    "scent", "item_form", "unit_count", "fragrance_free"]
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
        if isinstance(value, bool):
            clauses.append("p.attributes #> ARRAY[%s,'value'] = %s::jsonb")
            values.extend((key, "true" if value else "false"))
        else:
            clauses.append("lower(p.attributes #>> ARRAY[%s,'value'])=lower(%s)")
            values.extend((key, value))
    if filters.exclude_product_ids:
        clauses.append("NOT (p.product_id=ANY(%s))")
        values.append(filters.exclude_product_ids)
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


def search_catalog(db, request, embedder, pinned=None):
    pinned = pinned or pin_search_build(db)
    config = pinned["config"]
    if embedder.settings.model != config["model"]:
        raise CatalogError("Configured embedding model differs from the search build; rebuild first")
    suffix, params = filter_sql(request.filters)
    eligible_sql = "SELECT p.product_id FROM commerce.catalog_products p WHERE p.import_id=%s" + suffix
    eligible_count = db.execute("SELECT count(*) FROM (" + eligible_sql + ") eligible", [pinned["import_id"], *params]).fetchone()[0]
    base = {"query": request.query, "run_id": pinned["run_id"], "build_id": pinned["build_id"],
        "model": config["model"], "model_digest": config["model_digest"],
        "filters_applied": request.filters.model_dump(mode="json", exclude_none=True),
        "eligible_products": eligible_count, "method": "postgres_fts+exact_cosine+rrf",
        "constraints_source": "structured_filters",
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
    fused = reciprocal_rank_fusion(keyword, semantic)[:request.limit]
    records = dict(db.execute("SELECT product_id,document FROM commerce.catalog_products WHERE import_id=%s AND product_id=ANY(%s)",
                              (pinned["import_id"], [item[0] for item in fused])).fetchall())
    for product_id, ranking in fused:
        record = records[product_id]
        base["results"].append({"product_id": product_id, "title": record["title"],
            "category": record["category"], "brand": record["brand"], "price": record["price"],
            "attributes": record["attributes"], "availability": record["availability"],
            "image_urls": record["image_urls"][:3], "selected_observation": record["selected_observation"],
            "ranking": ranking, "listing_evidence": listing_evidence(record, request.query)})
    return base


class ProductSearch:
    def __init__(self, database_url=None, settings=None):
        self.database_url = database_url or os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce")
        self.settings = settings or EmbeddingSettings.from_env()

    def search(self, request):
        with connect_database(self.database_url) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            pinned = pin_search_build(db)
            with OllamaEmbedder(self.settings) as embedder:
                return search_catalog(db, request, embedder, pinned)

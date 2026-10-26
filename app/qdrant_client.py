"""Small bounded REST client for the pinned Qdrant deployment; no model runtime."""

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

from app.catalog import CatalogError
from app.embedding_text import DIMENSIONS
from app.storefronts import STOREFRONTS

# Changing text construction, tokenization, or taxonomy creates a separate collection.
INDEX_CONTRACT = {
    "version": 1,
    "dense": {"size": DIMENSIONS, "distance": "Cosine"},
    "lexical": {"model": "qdrant/bm25", "options": {"language": "english"}},
    "text": "title + newline + body",
    "storefronts": STOREFRONTS,
}
CONTRACT_SHA = hashlib.sha256(json.dumps(INDEX_CONTRACT, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class QdrantSettings:
    url: str = "http://127.0.0.1:6333"
    api_key: str = field(default="", repr=False)
    prefix: str = "commerce"
    timeout: float = 15
    exact: bool = True
    max_eligible: int = 100000

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise CatalogError("QDRANT_URL must be an HTTP(S) origin without credentials")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise CatalogError("Remote Qdrant connections require HTTPS")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", self.prefix):
            raise CatalogError("Invalid QDRANT_COLLECTION_PREFIX")
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise CatalogError("QDRANT_TIMEOUT_SECONDS must be between 1 and 120")
        if type(self.exact) is not bool or not 1 <= self.max_eligible <= 100000:
            raise CatalogError("Invalid Qdrant search bounds")

    @classmethod
    def from_env(cls):
        if os.getenv("SEARCH_BACKEND", "qdrant") != "qdrant":
            raise CatalogError(
                "PostgreSQL retrieval is retired; remove SEARCH_BACKEND from configuration"
            )
        try:
            exact = os.getenv("QDRANT_EXACT", "1")
            if exact not in {"0", "1"}:
                raise ValueError
            return cls(
                url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
                api_key=os.getenv("QDRANT_API_KEY", ""),
                prefix=os.getenv("QDRANT_COLLECTION_PREFIX", "commerce"),
                timeout=float(os.getenv("QDRANT_TIMEOUT_SECONDS", "15")),
                exact=exact == "1",
                max_eligible=int(os.getenv("QDRANT_MAX_ELIGIBLE", "100000")),
            )
        except ValueError as exc:
            if isinstance(exc, CatalogError):
                raise
            raise CatalogError("Invalid Qdrant configuration") from None

    def collection(self, build_id):
        if not re.fullmatch(r"[0-9a-f]{64}", build_id):
            raise CatalogError("Invalid search build ID")
        return f"{self.prefix}_{CONTRACT_SHA[:12]}_{build_id}"


def point_id(product_id):
    return str(uuid5(NAMESPACE_URL, "querix:product:" + product_id))


def lexical_document(text):
    return {"text": text, **INDEX_CONTRACT["lexical"]}


class QdrantClient:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.url.rstrip("/"),
            headers={"api-key": settings.api_key} if settings.api_key else {},
            timeout=settings.timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def request(self, method, path, body=None, *, missing_ok=False):
        try:
            response = self.http.request(method, path, json=body)
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "ok" or "result" not in data:
                raise ValueError
            return data["result"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Provider responses/URLs can contain credentials; never propagate them.
            raise CatalogError(
                "Qdrant request failed; check service, version, and index readiness"
            ) from None

    def info(self, collection, *, missing_ok=False):
        return self.request("GET", f"/collections/{collection}", missing_ok=missing_ok)

    def count(self, collection):
        return self.request("POST", f"/collections/{collection}/points/count", {"exact": True})[
            "count"
        ]

    def create(self, collection):
        self.request(
            "PUT",
            f"/collections/{collection}",
            {
                "vectors": {"dense": INDEX_CONTRACT["dense"]},
                "sparse_vectors": {"lexical": {"modifier": "idf"}},
            },
        )
        self.ensure_payload_indexes(collection)

    def ensure_payload_indexes(self, collection):
        # Idempotent, including recovery after interrupted collection creation.
        for name in ("build_id", "import_id", "storefront_id", "family", "domain"):
            self.request(
                "PUT",
                f"/collections/{collection}/index?wait=true",
                {"field_name": name, "field_schema": "keyword"},
            )

    def upsert(self, collection, points):
        result = self.request(
            "PUT", f"/collections/{collection}/points?wait=true", {"points": points}
        )
        if result.get("status") != "completed":
            raise CatalogError("Qdrant did not acknowledge the indexing batch")

    def retrieve(self, collection, ids, *, vectors=False):
        return self.request(
            "POST",
            f"/collections/{collection}/points",
            {"ids": ids, "with_payload": True, "with_vector": vectors},
        )

    def branches(self, collection, pinned, request, vector, eligible_ids):
        if not eligible_ids:
            return [], []
        if len(eligible_ids) > self.settings.max_eligible:
            raise CatalogError("Qdrant eligibility limit exceeded; narrow the search filters")
        must = [
            {"has_id": [point_id(key) for key in eligible_ids]},
            {"key": "build_id", "match": {"value": pinned["build_id"]}},
            {"key": "import_id", "match": {"value": pinned["import_id"]}},
        ]
        for field_name in ("storefront_id", "family", "domain"):
            value = getattr(request.filters, field_name)
            if value is not None:
                must.append({"key": field_name, "match": {"value": value}})
        common = {"filter": {"must": must}, "limit": 50, "with_payload": True, "with_vector": False}
        results = self.request(
            "POST",
            f"/collections/{collection}/points/query/batch",
            {
                "searches": [
                    {**common, "query": lexical_document(request.query), "using": "lexical"},
                    {
                        **common,
                        "query": vector,
                        "using": "dense",
                        "params": {"exact": self.settings.exact, "hnsw_ef": 128},
                    },
                ]
            },
        )
        allowed = set(eligible_ids)
        try:
            if len(results) != 2:
                raise ValueError
            branches = []
            for result in results:
                rows, seen = [], set()
                if len(result["points"]) > 50:
                    raise ValueError
                for hit in result["points"]:
                    payload = hit["payload"]
                    key, score = payload["product_id"], hit["score"]
                    if (
                        key not in allowed
                        or key in seen
                        or hit["id"] != point_id(key)
                        or payload["build_id"] != pinned["build_id"]
                        or payload["import_id"] != pinned["import_id"]
                        or payload["contract_sha"] != CONTRACT_SHA
                        or type(score) not in (float, int)
                        or not math.isfinite(score)
                    ):
                        raise ValueError
                    rows.append((key, float(score)))
                    seen.add(key)
                branches.append(sorted(rows, key=lambda item: (-item[1], item[0])))
            # Existing rank fusion expects cosine distance, not similarity.
            return branches[0], [(key, 1 - score) for key, score in branches[1]]
        except (KeyError, ValueError, TypeError):
            raise CatalogError("Qdrant returned invalid or out-of-scope candidates") from None

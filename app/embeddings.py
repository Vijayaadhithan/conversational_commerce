"""Local Ollama embeddings with explicit model identity and validated vectors."""

from dataclasses import dataclass
import math
import os
import re

import httpx

from app.catalog import CatalogError
from app.embedding_text import DIMENSIONS


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise CatalogError("Embedding response count differs from the request")
    result = []
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != DIMENSIONS
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
            raise CatalogError("Embedding response has invalid dimensions or non-finite values")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm < 1e-12:
            raise CatalogError("Embedding response contains a zero or invalid vector")
        result.append([value / norm for value in vector])
    return result


def vector_literal(vector):
    return "[" + ",".join(format(value, ".9g") for value in vector) + "]"


@dataclass(frozen=True)
class EmbeddingSettings:
    url: str = "http://127.0.0.1:11434"
    model: str = "embeddinggemma:latest"
    timeout: float = 120
    batch_size: int = 32

    @classmethod
    def from_env(cls):
        settings = cls(url=os.getenv("OLLAMA_BASE_URL", cls.url).rstrip("/"),
                       model=os.getenv("EMBEDDING_MODEL", cls.model),
                       timeout=float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "120")),
                       batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "32")))
        if not 1 <= settings.batch_size <= 128 or settings.timeout <= 0:
            raise CatalogError("Embedding batch size must be 1-128 and timeout must be positive")
        return settings


class OllamaEmbedder:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(base_url=settings.url, timeout=httpx.Timeout(settings.timeout, connect=5),
                                   trust_env=False, transport=transport)
        self.digest = None

    def __enter__(self):
        try:
            self.verify_identity()
        except Exception:
            self.client.close()
            raise
        return self

    def __exit__(self, *args):
        self.client.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("error"):
                raise ValueError("Invalid response")
            return value
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogError("Local embedding service failed; check Ollama and the configured installed model") from exc

    def verify_identity(self, expected=None):
        tags = self._request("GET", "/api/tags").get("models", [])
        if not isinstance(tags, list) or any(not isinstance(item, dict) for item in tags):
            raise CatalogError("Invalid model list from the local embedding service")
        match = next((item for item in tags if item.get("name") == self.settings.model), None)
        digest = match.get("digest") if match else None
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CatalogError("Configured embedding model is not installed locally")
        if digest != (expected or self.digest or digest):
            raise CatalogError("Embedding model digest changed; rebuild the search catalog")
        self.digest = digest
        return digest

    def embed(self, texts):
        self.verify_identity()
        payload = self._request("POST", "/api/embed", json={"model": self.settings.model,
            "input": texts, "truncate": False, "keep_alive": "10m"})
        vectors = validate_vectors(payload.get("embeddings"), len(texts))
        self.verify_identity()
        return vectors

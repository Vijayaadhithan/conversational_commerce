"""Lazy, offline-only local reranking. Heavy libraries load only when requested."""

import math
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "mixedbread-ai/mxbai-rerank-large-v2"
MODEL_REVISION = "ca7e1ee484c37c0ddd8d178a9a5c33cec575c5e6"
DEFAULT_MODEL_PATH = ROOT / "models/mxbai-rerank-large-v2"


class LocalReranker:
    def __init__(self):
        self.model = None
        self.device = None
        self.device_note = None
        self.batch_size = int(os.getenv("RERANK_BATCH_SIZE", "2"))
        self.max_length = int(os.getenv("RERANK_MAX_LENGTH", "512"))
        if not 1 <= self.batch_size <= 8 or not 128 <= self.max_length <= 2048:
            raise RuntimeError("Reranker batch size must be 1-8 and max length must be 128-2048")
        self.lock = threading.Lock()

    def _load(self):
        if self.model is not None:
            return
        path = Path(os.getenv("RERANK_MODEL_PATH", str(DEFAULT_MODEL_PATH)))
        if not path.is_absolute():
            path = ROOT / path
        required = ("model.safetensors", "config.json", "modules.json", "1_LogitScore/config.json")
        if not all((path / filename).is_file() for filename in required):
            raise RuntimeError("Local reranker is missing. See README.md for its download command.")
        try:
            import torch
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise RuntimeError(
                "Install requirements-reranker.txt to enable local reranking."
            ) from exc
        device = os.getenv("RERANK_DEVICE", "auto")
        if device not in {"auto", "cpu", "mps"}:
            raise RuntimeError("RERANK_DEVICE must be auto, cpu, or mps")
        if device == "auto":
            device = "cpu"
            if torch.backends.mps.is_available():
                try:
                    probe = torch.ones((2, 2), device="mps")
                    (probe @ probe).sum().item()
                    torch.mps.synchronize()
                    device = "mps"
                except RuntimeError:
                    self.device_note = "MPS operation failed; selected CPU."
            else:
                self.device_note = "MPS unavailable in this process; selected CPU."
        # Half precision bounds memory for the 1.54B-parameter model on Apple GPU.
        # Use float32 on CPU, which avoids depending on half-precision CPU kernels.
        model = CrossEncoder(
            str(path),
            device=device,
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={
                "dtype": torch.float16 if device == "mps" else torch.float32,
                "attn_implementation": "sdpa",
            },
            # Preserve ordering among strong matches: sigmoid in float16 rounds
            # large scores to 1.0 and can create artificial ties.
            activation_fn=torch.nn.Identity(),
            max_length=self.max_length,
        )
        self.model = model
        self.device = device

    def rank(self, query, documents, top_k, *, preserve_query=False):
        with self.lock:
            self._load()
            # The template places query before document. Reserve its full UTF-8
            # byte length plus template/suffix headroom so right truncation cannot
            # silently shorten an accepted product-search query (at most 1000 bytes).
            pair_length = (
                max(self.max_length, len(query.encode("utf-8")) + 256)
                if preserve_query
                else self.max_length
            )
            if preserve_query and self.model.tokenizer.truncation_side != "right":
                raise RuntimeError(
                    "Product reranking requires right truncation to preserve query text"
                )
            kwargs = (
                {"processing_kwargs": {"text": {"max_length": pair_length, "truncation": True}}}
                if preserve_query
                else {}
            )
            scores = self.model.predict(
                [(query, doc) for doc in documents],
                batch_size=self.batch_size,
                show_progress_bar=False,
                **kwargs,
            )
            # Reject invalid scores instead of returning a misleading ordering.
            if not all(math.isfinite(float(score)) for score in scores):
                raise RuntimeError("Local reranker returned non-finite scores")
            ranked = sorted(enumerate(scores), key=lambda item: float(item[1]), reverse=True)
            return {
                "model": MODEL_ID,
                "revision": MODEL_REVISION,
                "device": self.device,
                "max_pair_tokens": pair_length,
                "device_note": self.device_note,
                "results": [
                    {"index": index, "score": float(score), "text": documents[index]}
                    for index, score in ranked[:top_k]
                ],
            }


class VoyageReranker:
    """Hosted reranking with full index/score validation and no silent truncation."""

    provider = "voyage"
    device = "api"
    device_note = None

    def __init__(self, transport=None):
        from app.providers import credential

        self.model = os.getenv("VOYAGE_RERANK_MODEL", "rerank-2.5")
        self.timeout = float(os.getenv("RERANK_TIMEOUT_SECONDS", "60"))
        if not self.model.strip() or not math.isfinite(self.timeout) or not 0 < self.timeout <= 600:
            raise ValueError("Set a reranker model and a finite timeout between 0 and 600 seconds")
        credential("voyage")
        self.transport = transport

    def rank(self, query, documents, top_k, *, preserve_query=False):
        import httpx

        from app.providers import credential

        if not documents or top_k < 1:
            raise ValueError("Reranking requires documents and a valid top_k")
        top_k = min(top_k, len(documents))
        try:
            with httpx.Client(
                base_url="https://api.voyageai.com/v1/",
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
                headers={"Authorization": "Bearer " + credential("voyage")},
                timeout=httpx.Timeout(self.timeout, connect=5),
            ) as client:
                response = client.post(
                    "rerank",
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": documents,
                        "top_k": top_k,
                        "return_documents": False,
                        "truncation": False,
                    },
                )
                response.raise_for_status()
                rows = response.json()["data"]
            if not isinstance(rows, list) or len(rows) != top_k:
                raise ValueError("Invalid ranking count")
            seen, results = set(), []
            for row in rows:
                index, score = row["index"], row["relevance_score"]
                if (
                    type(index) is not int
                    or not 0 <= index < len(documents)
                    or index in seen
                    or type(score) not in (float, int)
                    or not math.isfinite(score)
                ):
                    raise ValueError("Invalid ranking result")
                seen.add(index)
                results.append({"index": index, "score": float(score), "text": documents[index]})
            results.sort(key=lambda row: (-row["score"], row["index"]))
            return {
                "model": self.model,
                "revision": None,
                "device": "api",
                "device_note": None,
                "max_pair_tokens": None,
                "results": results,
            }
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise RuntimeError(
                "Voyage reranking failed; check credentials, quota, input limits and timeout"
            ) from None


def create_reranker():
    provider = os.getenv("RERANK_PROVIDER", "local")
    if provider == "local":
        return LocalReranker()
    if provider == "voyage":
        return VoyageReranker()
    raise ValueError("RERANK_PROVIDER must be local or voyage")

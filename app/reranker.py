"""Lazy, offline-only local reranking. Heavy libraries load only when requested."""

import math
import os
from pathlib import Path
import threading

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
            raise RuntimeError("Install requirements-reranker.txt to enable local reranking.") from exc
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
            str(path), device=device, local_files_only=True, trust_remote_code=False,
            model_kwargs={"dtype": torch.float16 if device == "mps" else torch.float32,
                          "attn_implementation": "sdpa"},
            # Preserve ordering among strong matches: sigmoid in float16 rounds
            # large scores to 1.0 and can create artificial ties.
            activation_fn=torch.nn.Identity(),
            max_length=self.max_length,
        )
        self.model = model
        self.device = device

    def rank(self, query, documents, top_k):
        with self.lock:
            self._load()
            scores = self.model.predict([(query, doc) for doc in documents],
                                        batch_size=self.batch_size, show_progress_bar=False)
            # Reject invalid scores instead of returning a misleading ordering.
            if not all(math.isfinite(float(score)) for score in scores):
                raise RuntimeError("Local reranker returned non-finite scores")
            ranked = sorted(enumerate(scores), key=lambda item: float(item[1]), reverse=True)
            return {"model": MODEL_ID, "device": self.device,
                    "device_note": self.device_note,
                    "results": [{"index": index, "score": float(score), "text": documents[index]}
                                for index, score in ranked[:top_k]]}

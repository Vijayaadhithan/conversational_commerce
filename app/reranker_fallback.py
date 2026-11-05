"""Small, pinned, offline CPU reranker for hosted-provider outages."""

import hashlib
import importlib.util
import json
import math
import os
import threading
from pathlib import Path

from fastapi import HTTPException

from app.request_runtime import check_deadline

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "models/minilm-reranker"


class CpuFallbackReranker:
    """No network access or model download during a request."""

    def __init__(self):
        self.path = Path(os.getenv("RERANK_FALLBACK_MODEL_PATH", str(DEFAULT_PATH)))
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.session = None
        self.tokenizer = None
        self.lock = threading.Lock()

    def check_available(self):
        if not all(
            (self.path / f).is_file() for f in ("model.onnx", "tokenizer.json", "manifest.json")
        ):
            raise RuntimeError("Download the CPU fallback reranker before enabling it")
        if any(importlib.util.find_spec(m) is None for m in ("onnxruntime", "tokenizers", "numpy")):
            raise RuntimeError("Install requirements-fallback.lock before enabling fallback")
        return {"model": MODEL_ID, "revision": MODEL_REVISION, "loaded": self.session is not None}

    def _load(self):
        if self.session is not None:
            return
        self.check_available()
        manifest = json.loads((self.path / "manifest.json").read_text())
        if manifest.get("model") != MODEL_ID or manifest.get("revision") != MODEL_REVISION:
            raise RuntimeError("CPU reranker model identity does not match the pinned revision")
        for filename in ("model.onnx", "tokenizer.json"):
            with (self.path / filename).open("rb") as source:
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
            if manifest.get("sha256", {}).get(filename) != checksum:
                raise RuntimeError("CPU reranker checksum verification failed")
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        ort.disable_telemetry_events()
        session = ort.InferenceSession(
            str(self.path / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
        )
        tokenizer = Tokenizer.from_file(str(self.path / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=512, strategy="only_second")
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self.tokenizer, self.session = tokenizer, session

    def rank(self, query, documents, top_k, *, preserve_query=False):
        import numpy as np

        if not documents or top_k < 1:
            raise ValueError("Reranking requires documents and a valid top_k")
        with self.lock:
            check_deadline()
            self._load()
            scores = []
            for start in range(0, len(documents), 2):
                check_deadline()
                # Truncate documents only. An oversized query fails instead of
                # silently discarding the user's constraints.
                try:
                    encoded = self.tokenizer.encode_batch(
                        [(query, doc) for doc in documents[start : start + 2]]
                    )
                except Exception:
                    raise RuntimeError("Query does not fit the CPU reranker context") from None
                arrays = {
                    "input_ids": [item.ids for item in encoded],
                    "attention_mask": [item.attention_mask for item in encoded],
                    "token_type_ids": [item.type_ids for item in encoded],
                }
                inputs = {
                    item.name: np.asarray(arrays[item.name], dtype=np.int64)
                    for item in self.session.get_inputs()
                }
                logits = self.session.run(None, inputs)[0]
                batch_scores = np.asarray(logits).reshape(-1).tolist()
                if len(batch_scores) != len(encoded) or not all(
                    math.isfinite(score) for score in batch_scores
                ):
                    raise RuntimeError("CPU reranker returned invalid scores")
                scores.extend(batch_scores)
            check_deadline()
            indices = sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:top_k]
            return {
                "model": MODEL_ID,
                "revision": MODEL_REVISION,
                "device": "cpu",
                "device_note": "Hosted reranker unavailable; local CPU fallback used.",
                "max_pair_tokens": 512,
                "results": [
                    {"index": i, "score": scores[i], "text": documents[i]} for i in indices
                ],
            }


class RerankerWithFallback:
    provider = "voyage"
    device = "api"
    device_note = None

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.model = primary.model
        self.fallback_calls = 0
        self.last_backend = None

    def rank(self, query, documents, top_k, *, preserve_query=False):
        try:
            result = self.primary.rank(query, documents, top_k, preserve_query=preserve_query)
        except HTTPException as exc:
            # Never bypass application spending caps or an expired turn deadline.
            if exc.status_code != 503:
                raise
        except RuntimeError:
            pass
        else:
            self.last_backend = "voyage"
            return result
        check_deadline()
        result = self.fallback.rank(query, documents, top_k, preserve_query=preserve_query)
        self.fallback_calls += 1
        self.last_backend = "local"
        return result

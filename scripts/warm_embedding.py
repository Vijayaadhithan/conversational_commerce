#!/usr/bin/python3
"""Keep the local commerce embedding model resident between user requests."""

import json
import os
import time
import urllib.request


def main():
    model = os.environ.get("COMMERCE_WARM_MODEL", "embeddinggemma:latest")
    payload = json.dumps(
        {
            "model": model,
            "input": ["task: search result | query: moisturizer"],
            "truncate": False,
            "keep_alive": "10m",
        }
    ).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=75) as response:
        result = json.load(response)
    vectors = result.get("embeddings") or []
    if len(vectors) != 1 or len(vectors[0]) != 768:
        raise RuntimeError("Embedding warm-up returned invalid dimensions")
    print(
        json.dumps(
            {
                "event": "commerce_embedding_warmup",
                "model": model,
                "seconds": round(time.perf_counter() - started, 4),
                "load_seconds": round(result.get("load_duration", 0) / 1e9, 4),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

"""Run real local inference and save the observed runtime, device, and ranking."""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.reranker import LocalReranker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/reranker_check.json")
    args = parser.parse_args()
    engine = LocalReranker()
    started = time.perf_counter()
    cases = [
        ("fragrance free moisturizer for dry skin", [
            "A leather shoulder bag with adjustable strap.",
            "Fragrance-free face moisturizer for dry skin with glycerin and ceramides.",
            "A floral eau de parfum spray.",
        ], 1),
        ("blue cotton t shirt", [
            "A red leather handbag.",
            "A facial moisturizer for dry skin.",
            "A blue cotton crew neck t-shirt with short sleeves.",
        ], 2),
    ]
    results = []
    for query, documents, expected in cases:
        result = engine.rank(query, documents, top_k=3)
        if result["results"][0]["index"] != expected:
            raise AssertionError(f"Unexpected ranking for: {query}")
        results.append({"query": query, **result})
    if not any(abs(item["score"]) > 1 for result in results for item in result["results"]):
        raise AssertionError("Expected raw relevance scores rather than sigmoid probabilities")
    report = {"passed": True, "device": engine.device, "device_note": engine.device_note,
              "elapsed_seconds": time.perf_counter() - started, "cases": results}
    target = Path(args.output)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()

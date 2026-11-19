"""Prepare or score a per-turn review of the first five cards shown to a shopper."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.conversation_evaluation import dialogue_metrics


def worksheet(capture, digest):
    turns = []
    metrics = {}
    for case in capture["cases"]:
        section = case["storefront_id"]
        metrics.setdefault(section, []).append(case)
        for index, observation in enumerate(case["observations"], 1):
            commerce = observation.get("response", {}).get("commerce", {})
            cards = []
            for product in commerce.get("products", [])[:5]:
                cards.append(
                    {
                        "id": product["product_id"],
                        "title": product["title"],
                        "price": product["price"],
                        "explanation": product.get("why_recommended"),
                        "listing_evidence": product.get("listing_evidence", []),
                        "concern_evidence": product.get("concern_evidence", []),
                        "attributes": {k: v for k, v in product.get("attributes", {}).items() if v},
                        "relevance": None,
                        "explanation_supported": None,
                    }
                )
            turns.append(
                {
                    "case": case["id"],
                    "turn": index,
                    "storefront": section,
                    "shopper_messages": case["turns"][:index],
                    "http_status": observation["http_status"],
                    "reply": commerce.get("reply"),
                    "cards": cards,
                    "unnecessary_question": None,
                    "note": "",
                }
            )
    return {
        "capture_sha256": digest,
        "reviewer": "",
        "human_reviewed": None,
        "instructions": "Grade every displayed card 0 (wrong item or violates a request), 1 (partial or uncertain fit), or 2 (listing supports the requested fit). Mark whether the card explanation is supported and whether the turn asks an unnecessary question. Use the full capture for earlier dialogue and evidence.",
        "metrics_by_storefront": {k: dialogue_metrics(v) for k, v in metrics.items()},
        "turns": turns,
    }


def score(expected, review):
    if review.get("capture_sha256") != expected["capture_sha256"]:
        raise ValueError("Review belongs to another capture")
    if (
        not str(review.get("reviewer") or "").strip()
        or type(review.get("human_reviewed")) is not bool
    ):
        raise ValueError("Declare the reviewer and whether the review was human")
    counts = {}
    for turn, labels in zip(expected["turns"], review["turns"], strict=True):
        if any(turn[k] != labels[k] for k in ("case", "turn", "storefront")):
            raise ValueError("Turn identity or order differs from capture")
        values = counts.setdefault(turn["storefront"], Counter())
        question = labels["unnecessary_question"]
        if type(question) is not bool:
            raise ValueError("Label each turn's question")
        values["turns"] += 1
        values["unnecessary_questions"] += question
        for card, labeled in zip(turn["cards"], labels["cards"], strict=True):
            grade = labeled["relevance"]
            supported = labeled["explanation_supported"]
            if (
                card["id"] != labeled["id"]
                or type(grade) is not int
                or grade not in (0, 1, 2)
                or type(supported) is not bool
            ):
                raise ValueError(
                    "Each ranked card needs matching ID, relevance and evidence labels"
                )
            values["cards"] += 1
            values[("wrong", "partial", "strong")[grade]] += 1
            values["unsupported_explanations"] += not supported
    return {
        "capture_sha256": expected["capture_sha256"],
        "reviewer": review["reviewer"],
        "human_reviewed": review["human_reviewed"],
        "by_storefront": counts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.capture.read_bytes()
    result = worksheet(json.loads(raw), hashlib.sha256(raw).hexdigest())
    if args.review:
        result = score(result, json.loads(args.review.read_text()))
    with args.output.open("x") as target:
        json.dump(result, target, indent=2)
        target.write("\n")

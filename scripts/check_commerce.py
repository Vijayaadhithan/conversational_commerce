"""Exercise the real local intent model, catalog, RRF and reranker through the API.

Uses an isolated temporary conversation database, automatically removed on exit.
This checks continuity and constraints, not human-judged recommendation quality.
"""

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation_client import evaluation_client

from app.config import ROOT, Settings


def main():
    observations = []
    with TemporaryDirectory(prefix="commerce-check-") as temporary:
        settings = replace(Settings(), database=str(Path(temporary) / "conversations.sqlite3"))
        with evaluation_client(settings) as client:
            session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]

            def send(message):
                start = time.perf_counter()
                response = client.post(
                    f"/conversations/{session}/messages", json={"message": message}
                )
                assert response.status_code == 200, (
                    f"{message}: {response.status_code} {response.text}"
                )
                value = response.json()["commerce"]
                active = next(
                    (
                        task
                        for task in value["preferences"]["tasks"]
                        if task["id"] == value["preferences"]["active_task_id"]
                    ),
                    None,
                )
                if value["action"] == "recommend":
                    filters = value["search"]["filters_applied"]
                    for row in value["products"]:
                        assert row["why_recommended"]
                        if filters.get("max_price") is not None:
                            assert Decimal(row["price"]["amount"]) <= Decimal(filters["max_price"])
                        for key, claim in filters.get("attributes", {}).items():
                            assert (
                                row["attributes"][key] and row["attributes"][key]["value"] == claim
                            )
                        assert row["product_id"] not in filters.get("exclude_product_ids", [])
                    if value["products"]:
                        assert value["search"]["reranking"]["applied"]
                observations.append(
                    {
                        "message": message,
                        "seconds": round(time.perf_counter() - start, 3),
                        "intent": value["intent"],
                        "action": value["action"],
                        "active_task": active,
                        "products": [
                            {
                                "product_id": row["product_id"],
                                "title": row["title"],
                                "price": row["price"],
                                "why_recommended": row["why_recommended"],
                            }
                            for row in value["products"]
                        ],
                        "search": value["search"],
                    }
                )
                print(
                    f"Passed: {message} -> {value['action']}, {len(value['products'])} products ({observations[-1]['seconds']:.2f}s)",
                    flush=True,
                )
                print(
                    f"  Intent: {json.dumps(value['intent'])}; goal: {active['query'] if active else '<none>'}; concern: {active['need'] if active else '<none>'}",
                    flush=True,
                )
                return value, active

            value, task = send("I have dry skin")
            assert value["action"] == "clarify" and not task["query"] and "dry skin" in task["need"]
            saved_concern = task["need"]
            value, task = send("Show me a moisturizer under $30")
            assert (
                value["products"]
                and task["filters"]["max_price"] == "29.99"
                and task["need"] == saved_concern
            )
            value, task = send("Under $20 instead")
            assert (
                len(value["products"]) >= 2
                and task["filters"]["max_price"] == "19.99"
                and task["need"] == saved_concern
            )
            original = [row["product_id"] for row in value["products"]]
            value, task = send("Compare the first two")
            assert (
                value["action"] == "compare"
                and [row["product_id"] for row in value["products"]] == original[:2]
            )
            value, task = send("More options")
            assert value["products"] and set(original).isdisjoint(
                row["product_id"] for row in value["products"]
            )
            second = value["products"][1]["product_id"]
            value, task = send("Details about the second")
            assert value["products"][0]["product_id"] == second
            value, task = send("Now I also need a shirt")
            assert (
                value["action"] == "clarify"
                and task["filters"]["family"] == "apparel"
                and task["filters"]["max_price"] is None
            )
            value, task = send("Back to the moisturizer")
            assert (
                task["id"] == 1
                and task["filters"]["max_price"] == "19.99"
                and task["need"] == saved_concern
            )
            value, task = send("Make it fragrance-free")
            assert task["filters"]["attributes"]["fragrance_free"] is True
            expected = client.get(f"/conversations/{session}/preferences").json()
        # A new application instance must read the same structured state and evidence.
        with evaluation_client(settings) as restarted:
            assert restarted.get(f"/conversations/{session}/preferences").json() == expected
            transcript = restarted.get(f"/conversations/{session}/messages").json()["messages"]
            assert len(transcript) == 2 * len(observations)
            assert transcript[3]["commerce"]["products"]
    output = ROOT / "data/search/commerce_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "passed": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "evaluation_type": "one scripted conversation and restart; state, provenance and filter checks; no relevance qrels",
                "turns": len(observations),
                "observations": observations,
            },
            indent=2,
        )
    )
    print(f"Passed {len(observations)} live turns and restart. Report: {output}")


if __name__ == "__main__":
    main()

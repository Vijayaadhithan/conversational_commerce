"""Exercise the local browser gateway against real commerce models and catalog.

Creates and deletes only this check's conversations. No credentials are read or
printed: the same cookie and Origin contract as the browser is used.
"""

import argparse
import json
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    parser.add_argument("--output", type=Path, default=Path("data/frontend/live_report.json"))
    args = parser.parse_args()
    results = []
    summary = None

    def call(client, method, path, **kwargs):
        response = client.request(method, "/api/commerce/" + path, **kwargs)
        if not response.is_success:
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}")
        return response.json()

    def turn(client, message, *, key=None):
        start = time.monotonic()
        result = call(
            client,
            "POST",
            "messages",
            json={"message": message},
            headers={"Idempotency-Key": key or str(uuid4())},
        )
        commerce = result["commerce"]
        products = commerce.get("products", [])
        assert all(row["product_id"].startswith("amazon:") for row in products)
        assert all(row["price"]["currency"] == "USD" for row in products)
        if commerce.get("search"):
            assert commerce["search"]["build_id"] == summary["build_id"]
        entry = {
            "message": message,
            "action": commerce["action"],
            "products": len(products),
            "seconds": round(time.monotonic() - start, 2),
            "task_budgets": {
                task["query"]: task["filters"].get("max_price")
                for task in commerce["preferences"]["tasks"]
            },
        }
        results.append(entry)
        print(json.dumps(entry), flush=True)
        return result

    def session(client):
        assert call(client, "GET", "status")["available"]
        call(client, "POST", "session")

    try:
        with httpx.Client(base_url=args.url, headers={"Origin": args.url}, timeout=500) as client:
            session(client)
            try:
                summary = call(client, "GET", "catalog")
                assert summary["product_count"] == summary["indexed_product_count"] > 0
                print(
                    f"Catalog: {summary['product_count']} products, {len(summary['categories'])} categories",
                    flush=True,
                )
                result = turn(client, "I have dry skin")
                assert result["commerce"]["action"] == "clarify"
                result = turn(client, "Show me a moisturizer under $30")
                assert result["commerce"]["products"]
                key = str(uuid4())
                result = turn(client, "Under $20 instead", key=key)
                assert result["commerce"]["products"]
                assert all(
                    Decimal(p["price"]["amount"]) < 20 for p in result["commerce"]["products"]
                )
                replay = turn(client, "Under $20 instead", key=key)
                assert replay["message_id"] == result["message_id"]
                history = call(client, "GET", "history")
                assert len(history["messages"]) == 6
                assert (
                    history["messages"][-1]["commerce"]["products"]
                    == result["commerce"]["products"]
                )
                result = turn(client, "Compare the first two in task 1")
                assert result["commerce"]["action"] == "compare"
                product = result["commerce"]["products"][0]["product_id"]
                detail = turn(client, f"Details about {product} in task 1")
                assert detail["commerce"]["action"] == "details"
                assert detail["commerce"]["products"][0]["product_id"] == product
                before = call(client, "GET", "history")
                call(client, "POST", "session")
                assert call(client, "GET", "history") == before
                more = turn(client, "More options for task 1")
                assert more["commerce"]["action"] in {"recommend", "recommendations", "search"}
                empty = turn(client, "Moisturizer under $0.01")
                assert not empty["commerce"]["products"]
            finally:
                call(client, "DELETE", "session")
            assert client.get("/api/commerce/history").status_code == 401
            session(client)
            try:
                result = turn(client, "Shampoo under $15 and moisturizer under $25")
                assert len(result["commerce"].get("groups", [])) == 2
                tasks = result["commerce"]["preferences"]["tasks"]
                shampoo = next(task for task in tasks if "shampoo" in task["query"].lower())
                refined = turn(client, "Shampoo under $10 instead")
                tasks_after = refined["commerce"]["preferences"]["tasks"]
                for task in tasks_after:
                    original = next(item for item in tasks if item["id"] == task["id"])
                    if task["id"] != shampoo["id"]:
                        assert task["filters"] == original["filters"]
                with httpx.Client(
                    base_url=args.url, headers={"Origin": args.url}, timeout=30
                ) as other:
                    session(other)
                    try:
                        assert call(other, "GET", "history")["messages"] == []
                    finally:
                        call(other, "DELETE", "session")
            finally:
                call(client, "DELETE", "session")
            session(client)
            try:
                result = turn(client, "A cotton shirt for adult men under $40")
                assert result["commerce"]["products"]
                assert all(
                    Decimal(row["price"]["amount"]) < 40 for row in result["commerce"]["products"]
                )
            finally:
                call(client, "DELETE", "session")
        status = "passed"
    except Exception as exc:
        status = "failed"
        print(f"Check failed: {type(exc).__name__}: {exc}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"status": status, "catalog": summary, "turns": results}, indent=2)
    )
    print(f"Report: {args.output} ({status})", flush=True)
    if status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

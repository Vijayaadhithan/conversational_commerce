"""Replay typo-containing concern discovery through real local models and catalog.

The conversation database is temporary; existing user conversations are untouched.
"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import ROOT, Settings, create_app
from app.commerce_models import ShoppingState, ShoppingTask


def main():
    observations = []
    with TemporaryDirectory(prefix="commerce-concerns-") as temporary:
        settings = replace(Settings(), database=str(Path(temporary) / "conversations.sqlite3"))
        with TestClient(create_app(settings)) as client:
            session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]

            def send(message):
                start = time.perf_counter()
                response = client.post(f"/conversations/{session}/messages", json={"message": message})
                assert response.status_code == 200, f"{message}: {response.status_code} {response.text}"
                result = response.json()["commerce"]
                task = next(item for item in result["preferences"]["tasks"] if item["id"] == result["preferences"]["active_task_id"])
                observations.append({"message": message, "seconds": round(time.perf_counter() - start, 3), "result": result})
                print(f"Passed: {message} -> {result['action']}, concerns: {task['need']}", flush=True)
                return result, task

            first, task = send("i need a prduct that lightens my face")
            assert first["action"] == "clarify" and first["preferences"]["pending_kind"] == "skin_profile"
            assert "lighten" in task["need"] and not task["query"]
            result, task = send("that lighten my face, reduce pimble, and so on")
            assert result["action"] == "clarify" and "pimble" in task["need"] and "lighten" in task["need"]
            count = len(task["needs"])
            result, task = send("that lighten my face, reduce pimble, and so on")
            assert len(task["needs"]) == count
            result, task = send("oily skin, under $20, show me options")
            assert result["action"] == "recommend" and len(result["products"]) >= 2
            assert task["query"] == "skin care" and "pimble" in task["need"] and "lighten" in task["need"]
            assert result["search"]["filters_applied"]["max_price"] == "19.99"
            assert result["search"]["filters_applied"]["family"] == "skin_care"
            assert result["search"]["reranking"]["applied"]
            for row in result["products"]:
                assert row["category"]["family"] == "skin_care" and Decimal(row["price"]["amount"]) < 20
            ids = [row["product_id"] for row in result["products"]]
            result, task = send("Compare the first two")
            assert [row["product_id"] for row in result["products"]] == ids[:2]
            # Continue the shape saved by the older implementation's generic question.
            session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]
            legacy = ShoppingState(tasks=[ShoppingTask(id=1, need="lightens my face",
                sources={"need": {"value": "lightens my face", "evidence": "lightens my face", "source": "user"}})],
                active_task_id=1, turns=1, pending_kind="product_type",
                pending_question="What type of product are you looking for—for example a moisturizer, cleanser, shirt, or bag?")
            client.app.state.store.commit_commerce_turn(session, "i need a prduct that lightens my face", legacy,
                {"reply": legacy.pending_question}, 0)
            result, task = send("that lighten my face, reduce pimble, and so on")
            assert result["action"] == "clarify" and "pimble" in task["need"] and "lightens my face" in task["need"]
            expected = client.get(f"/conversations/{session}/preferences").json()
        with TestClient(create_app(settings)) as restarted:
            assert restarted.get(f"/conversations/{session}/preferences").json() == expected
    output = ROOT / "data/search/concern_flow_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"passed": True, "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_type": "one live multi-concern conversation, legacy continuation and restart; no human relevance qrels",
        "turns": len(observations), "observations": observations}, indent=2))
    print(f"Passed {len(observations)} live turns and restart. Report: {output}")


if __name__ == "__main__":
    main()

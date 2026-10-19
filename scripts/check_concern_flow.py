"""Replay typo-containing concern discovery through real local models and catalog.

The conversation database is temporary; existing user conversations are untouched.
"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.main import ROOT, Settings, create_app
from app.commerce_models import ShoppingState, ShoppingTask
from app.commerce import handle_turn, IntentError


def main():
    observations = []
    def observed_turn(saved, plan, message, search):
        try:
            return handle_turn(saved, plan, message, search)
        except (IntentError, ValidationError, InvalidOperation) as exc:
            print(f"Rejected synthetic-test proposal ({exc}): {plan.model_dump_json()}", flush=True)
            raise

    with TemporaryDirectory(prefix="commerce-concerns-") as temporary, patch("app.main.handle_turn", observed_turn):
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
            assert first["action"] in {"clarify", "recommend"}
            assert "lighten" in task["need"]
            if first["action"] == "clarify":
                assert first["preferences"]["pending_kind"] == "skin_profile" and not task["query"]
            else:
                assert task["query"] == "skin care" and task["sources"]["query"]["source"] == "concern_guided_discovery"
            result, task = send("something that is kess chemincal")
            assert result["action"] == "clarify" and "ingredient" in result["reply"]
            assert "chemincal" in task["need"] and "lighten" in task["need"]
            assert task["filters"]["attributes"] == {}
            result, task = send("that lighten my face, reduce pimble, and so on")
            assert result["action"] in {"clarify", "recommend"} and "pimble" in task["need"] and "lighten" in task["need"]
            if result["action"] == "recommend": assert result["intent"]["recommendation_requested"] is True
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
            assert result["action"] in {"clarify", "recommend"} and "pimble" in task["need"] and "lightens my face" in task["need"]
            if result["action"] == "recommend": assert result["intent"]["recommendation_requested"] is True
            # Multiple product types, including two from the same family, then a named follow-up.
            session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]
            result, task = send("shampoo under $15 and moisturizer under $25")
            assert result["action"] == "multi" and len(result["groups"]) == 2
            assert [item["query"] for item in result["preferences"]["tasks"]] == ["shampoo", "moisturizer"]
            assert [item["filters"]["max_price"] for item in result["preferences"]["tasks"]] == ["14.99", "24.99"]
            assert all(group["products"] for group in result["groups"])
            result, task = send("Compare the first two")
            assert result["action"] == "clarify" and result["preferences"]["pending_kind"] == "task_selection"
            result, task = send("shampoo under $10 instead")
            assert result["action"] == "recommend" and task["query"] == "shampoo"
            assert task["filters"]["max_price"] == "9.99"
            assert result["preferences"]["tasks"][1]["filters"]["max_price"] == "24.99"
            session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]
            result, task = send("cleanser under $20 and moisturizer under $30")
            assert result["action"] == "multi" and len(result["groups"]) == 2
            assert [item["query"] for item in result["preferences"]["tasks"]] == ["cleanser", "moisturizer"]
            expected = client.get(f"/conversations/{session}/preferences").json()
        with TestClient(create_app(settings)) as restarted:
            assert restarted.get(f"/conversations/{session}/preferences").json() == expected
    output = ROOT / "data/search/concern_flow_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"passed": True, "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_type": "live typo preferences, multi-product conversations, legacy continuation and restart; no human relevance qrels",
        "turns": len(observations), "observations": observations}, indent=2))
    print(f"Passed {len(observations)} live turns and restart. Report: {output}")


if __name__ == "__main__":
    main()

"""Check natural browsing requests against the real local planner and catalog."""

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation_client import evaluation_client

from app.commerce import IntentError, handle_turn
from app.commerce_models import ShoppingState, ShoppingTask
from app.config import ROOT, Settings


def main():
    observations = []

    def observed_turn(saved, plan, message, search):
        try:
            return handle_turn(saved, plan, message, search)
        except IntentError:
            print(f"Rejected synthetic-test proposal: {plan.model_dump_json()}", flush=True)
            raise

    with (
        TemporaryDirectory(prefix="commerce-browse-intent-") as temporary,
        patch("app.main.handle_turn", observed_turn),
    ):
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
                result = response.json()["commerce"]
                observations.append(
                    {
                        "message": message,
                        "seconds": round(time.perf_counter() - start, 3),
                        "result": result,
                    }
                )
                print(
                    f"Passed: {message} -> {result['action']}; browse={result['intent']['recommendation_requested']}",
                    flush=True,
                )
                return result

            send("i need a prduct that lightens my face")
            send("something that is kess chemincal")
            snapshot = ShoppingState.model_validate(
                client.app.state.store.get(session)["shopping_state"]
            )
            assert "lighten" in snapshot.active().need and "chemincal" in snapshot.active().need
            # Reproduce the reported unresolved state even if the improved
            # planner already offered broad discovery on the initial request.
            snapshot = ShoppingState(
                tasks=[
                    ShoppingTask(id=1, need=snapshot.active().need, needs=snapshot.active().needs)
                ],
                active_task_id=1,
                pending_kind="preference_detail",
                pending_question="Which ingredient do you want to avoid? You can also ask for related products now.",
            )
            # Start each phrasing from the same unresolved conversation, so an
            # earlier successful search cannot hide a failure to start discovery.
            for message in [
                "give me soem products with the above conditions",
                "what would you pick for me?",
                "anything matching what we discussed?",
                "don't ask more questions, give me a few choices based on that",
            ]:
                session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]
                client.app.state.store.commit_commerce_turn(
                    session,
                    "something that is kess chemincal",
                    snapshot,
                    {"reply": snapshot.pending_question},
                    0,
                )
                result = send(message)
                assert result["intent"]["recommendation_requested"] is True
                assert result["action"] == "recommend" and result["products"]
                assert result["intent_attempts"] == 1
                task = result["preferences"]["tasks"][0]
                assert "lighten" in task["need"] and "chemincal" in task["need"]
                assert (
                    task["filters"]["family"] == "skin_care" and task["filters"]["attributes"] == {}
                )
                assert all(row["category"]["family"] == "skin_care" for row in result["products"])

            for message in ["Don't suggest products yet", "What does 'show options' do?"]:
                result = send(message)
                assert result["intent"]["recommendation_requested"] is False
                assert result["products"] == [] and result["search"] is None
            expected = client.get(f"/conversations/{session}/preferences").json()
        with evaluation_client(settings) as restarted:
            assert restarted.get(f"/conversations/{session}/preferences").json() == expected
    output = ROOT / "data/search/recommendation_intent_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "passed": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "evaluation_type": "live recommendation-intent regression; no human relevance judgments",
                "turns": len(observations),
                "observations": observations,
            },
            indent=2,
        )
    )
    print(f"Passed {len(observations)} live turns and restart. Report: {output}")


if __name__ == "__main__":
    main()

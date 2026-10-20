"""Capture real synthetic conversations for explicit AI or human review."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evaluation_client import evaluation_client
from app.main import ROOT, Settings, create_app
from app.commerce_models import PLANNER_PROMPT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/conversations.json")
    parser.add_argument("--repetitions", type=int, default=1, choices=range(1, 6))
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cases.read_text())
    if args.repetitions > 1:
        cases = [{**case, "id": f"{case['id']}__run{run}"} for run in range(1, args.repetitions + 1) for case in cases]
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "review_type": "unreviewed synthetic conversations",
              "human_reviewed": False, "repetitions": args.repetitions, "chat_model": Settings().model,
              "planner_prompt_sha256": hashlib.sha256(PLANNER_PROMPT.encode()).hexdigest(),
              "code_sha256": {name: hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest()
                  for name in ("commerce.py", "commerce_models.py", "product_search.py", "listing_explanations.py")},
              "case_set_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(), "cases": []}
    with TemporaryDirectory(prefix="commerce-evaluation-") as tmp:
        with evaluation_client(replace(Settings(), database=str(Path(tmp) / "conversations.sqlite3"))) as client:
            for case in cases:
                session = client.post("/conversations", json={"mode": "commerce"}).json()["id"]
                observed = {**case, "observations": []}
                for message in case["turns"]:
                    started = time.perf_counter()
                    response = client.post(f"/conversations/{session}/messages", json={"message": message})
                    observed["observations"].append({"message": message, "http_status": response.status_code,
                        "seconds": round(time.perf_counter() - started, 4), "response": response.json(),
                        "preferences": client.get(f"/conversations/{session}/preferences").json()})
                    print(f"{case['id']}: HTTP {response.status_code}: {message}", flush=True)
                report["cases"].append(observed)
                output.write_text(json.dumps(report, indent=2))
    print(f"Saved {len(cases)} conversations to {output}")


if __name__ == "__main__": main()

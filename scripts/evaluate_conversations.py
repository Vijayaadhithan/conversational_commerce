"""Capture real synthetic conversations for explicit AI or human review."""

import argparse
import hashlib
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evaluation_client import evaluation_client

from app.commerce_prompts import DIALOGUE_PROMPT, PLANNER_PROMPT, SCOPE_PROMPT, STOREFRONT_PROMPT
from app.config import ROOT, Settings
from app.storefront_evaluation import check_storefront_response


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
        cases = [
            {**case, "id": f"{case['id']}__run{run}"}
            for run in range(1, args.repetitions + 1)
            for case in cases
        ]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "review_type": "unreviewed synthetic conversations",
        "human_reviewed": False,
        "repetitions": args.repetitions,
        "chat_model": Settings().model,
        "planner_prompt_sha256": hashlib.sha256(
            (PLANNER_PROMPT + SCOPE_PROMPT + STOREFRONT_PROMPT + DIALOGUE_PROMPT).encode()
        ).hexdigest(),
        "code_sha256": {
            name: hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest()
            for name in (
                "commerce.py",
                "commerce_models.py",
                "commerce_prompts.py",
                "commerce_taxonomy.py",
                "commerce_storefront.py",
                "storefronts.py",
                "search_normalization.py",
                "product_search.py",
                "listing_explanations.py",
            )
        },
        "case_set_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "cases": [],
    }
    with TemporaryDirectory(prefix="commerce-evaluation-") as tmp:
        with evaluation_client(
            replace(Settings(), database=str(Path(tmp) / "conversations.sqlite3"))
        ) as client:
            for case in cases:
                body = {"mode": "commerce"}
                if case.get("storefront_id"):
                    body["storefront_id"] = case["storefront_id"]
                created = client.post("/conversations", json=body)
                created.raise_for_status()
                session = created.json()["id"]
                observed = {**case, "observations": []}
                for turn_index, message in enumerate(case["turns"]):
                    started = time.perf_counter()
                    response = client.post(
                        f"/conversations/{session}/messages", json={"message": message}
                    )
                    observed["observations"].append(
                        {
                            "message": message,
                            "http_status": response.status_code,
                            "seconds": round(time.perf_counter() - started, 4),
                            "response": response.json(),
                            "storefront_checks": check_storefront_response(
                                case["storefront_id"],
                                response.json(),
                                case.get("expected_destinations") if turn_index == 0 else None,
                            )
                            if case.get("storefront_id")
                            else None,
                            "preferences": client.get(
                                f"/conversations/{session}/preferences"
                            ).json(),
                        }
                    )
                    print(f"{case['id']}: HTTP {response.status_code}: {message}", flush=True)
                report["cases"].append(observed)
                output.write_text(json.dumps(report, indent=2))
    print(f"Saved {len(cases)} conversations to {output}")


if __name__ == "__main__":
    main()

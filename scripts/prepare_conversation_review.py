"""Create an unscored review template for one captured conversation evaluation."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.conversation_evaluation import review_template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    review = review_template(json.loads(args.capture.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a person's existing judgments.
    with args.output.open("x") as output:
        json.dump(review, output, indent=2)
        output.write("\n")
    print(f"Prepared {len(review['cases'])} unscored cases; fill all labels before summarizing.")


if __name__ == "__main__":
    main()

"""Expand from the already downloaded normalized catalog, retaining the original sample."""

import argparse
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.catalog_sampling import expand_sample

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="../conversational_commerce_etl/data/processed")
    parser.add_argument("--output-dir", default="data/scaled")
    parser.add_argument("--size", type=int, default=50000)
    args = parser.parse_args()
    started = time.perf_counter()
    result = expand_sample(args.source_dir, args.output_dir, args.size)
    result.update(
        seconds=time.perf_counter() - started,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
    )
    Path(args.output_dir, "execution_report.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

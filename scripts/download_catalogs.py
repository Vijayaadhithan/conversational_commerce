"""Download only the three selected public metadata archives; validate and count rows."""

import gzip
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    sources = json.loads((ROOT / "data_sources.json").read_text())["sources"]
    destination = ROOT / "data/public_sources/raw"
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for source in sources:
        path = destination / source["filename"]
        temporary = path.with_suffix(path.suffix + ".part")
        if not path.exists():
            print(
                f"Downloading {source['name']} ({source['compressed_bytes']:,} bytes)", flush=True
            )
            subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "--retry",
                    "3",
                    "--connect-timeout",
                    "10",
                    "--continue-at",
                    "-",
                    "--output",
                    str(temporary),
                    source["url"],
                ],
                check=True,
            )
            if temporary.stat().st_size != source["compressed_bytes"]:
                raise ValueError(f"Unexpected archive size: {temporary}")
            temporary.replace(path)
        if path.stat().st_size != source["compressed_bytes"]:
            raise ValueError(f"Archive size mismatch: {path}")
        print(f"Validating {source['name']}", flush=True)
        rows, missing_id, categories = 0, 0, {}
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"Non-object metadata at row {rows + 1} in {path}")
                rows += 1
                missing_id += not bool(record.get("parent_asin"))
                category = str(record.get("main_category") or "unknown")
                categories[category] = categories.get(category, 0) + 1
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        result = {
            **source,
            "path": str(path.relative_to(ROOT)),
            "rows": rows,
            "missing_parent_asin": missing_id,
            "main_categories": categories,
            "sha256": checksum,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }
        results.append(result)
        # Preserve completed-source evidence if a later download needs retrying.
        (destination.parent / "download_report.json").write_text(
            json.dumps(results, indent=2) + "\n"
        )
        print(
            f"OK: {source['name']}: {rows:,} metadata rows; missing IDs: {missing_id}", flush=True
        )


if __name__ == "__main__":
    main()

"""Fetch two files from a fixed model revision; never download during requests."""

import hashlib
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.reranker_fallback import DEFAULT_PATH, MODEL_ID, MODEL_REVISION


def main():
    DEFAULT_PATH.mkdir(parents=True, exist_ok=True)
    manifest = {"model": MODEL_ID, "revision": MODEL_REVISION, "sha256": {}}
    with httpx.Client(follow_redirects=True, timeout=180, trust_env=False) as client:
        for remote, local in (
            ("onnx/model.onnx", "model.onnx"),
            ("tokenizer.json", "tokenizer.json"),
        ):
            target = DEFAULT_PATH / local
            temporary = target.with_suffix(".download")
            with client.stream(
                "GET", f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{remote}"
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
            temporary.replace(target)
            manifest["sha256"][local] = hashlib.sha256(target.read_bytes()).hexdigest()
            print(f"Downloaded {local}: {target.stat().st_size} bytes")
    (DEFAULT_PATH / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()

"""Download the pinned reranker into the ignored project model directory."""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reranker import DEFAULT_MODEL_PATH, MODEL_ID, MODEL_REVISION


if __name__ == "__main__":
    cli = Path(sys.executable).parent / "hf"
    if not cli.is_file():
        raise SystemExit("Install requirements-reranker.txt first to provide the hf CLI.")
    subprocess.run([str(cli), "download", MODEL_ID, "--revision", MODEL_REVISION,
                    "--local-dir", str(DEFAULT_MODEL_PATH)], check=True)

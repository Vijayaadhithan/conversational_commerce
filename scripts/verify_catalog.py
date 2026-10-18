"""Compare every imported record and typed projection with the ETL subset."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.catalog_cli import main

if __name__ == "__main__":
    raise SystemExit(main("verify"))

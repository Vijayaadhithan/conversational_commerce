"""Create the dedicated catalog schema (and optionally its database)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.catalog_cli import main

if __name__ == "__main__":
    raise SystemExit(main("init"))

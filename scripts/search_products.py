"""Search the local catalog directly without starting the HTTP server."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
import psycopg
from pydantic import ValidationError
from app.catalog import ROOT, CatalogError
from app.product_search import ProductSearch, ProductSearchRequest


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--filters", default="{}", help="JSON object with structured mandatory filters")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--no-rerank", action="store_true", help="Explicitly return the RRF baseline without local reranking")
    args = parser.parse_args()
    try:
        request = ProductSearchRequest(query=args.query, filters=json.loads(args.filters), limit=args.limit, rerank=not args.no_rerank)
        print(json.dumps(ProductSearch().search(request), indent=2, ensure_ascii=False))
        return 0
    except (CatalogError, ValidationError, ValueError, psycopg.Error, OSError) as exc:
        message = str(exc) if isinstance(exc, CatalogError) else "Check query/filter syntax, PostgreSQL, Ollama, and the completed search build"
        print(f"Product search failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

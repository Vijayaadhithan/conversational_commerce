"""Build/resume all active-catalog embeddings using the installed local model."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
import psycopg
from app.catalog import ROOT, CatalogError, connect_database
from app.embeddings import EmbeddingSettings, OllamaEmbedder
from app.search_index import build_index


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    refresh = parser.add_mutually_exclusive_group()
    refresh.add_argument("--reembed-all", action="store_true", help="Generate every vector in a new generation")
    refresh.add_argument("--reembed-product", action="append", default=[], metavar="PRODUCT_ID", help="Force selected product embeddings; repeat for multiple IDs")
    refresh.add_argument("--resume-build", help="Resume or verify a specific build, including a forced refresh")
    args = parser.parse_args()
    try:
        with connect_database(os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce"), autocommit=True) as db:
            with OllamaEmbedder(EmbeddingSettings.from_env()) as embedder:
                print(json.dumps(build_index(db, embedder, reembed_all=args.reembed_all,
                    reembed_ids=args.reembed_product, resume_build=args.resume_build), indent=2))
        return 0
    except (CatalogError, psycopg.Error, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, CatalogError) else "Check database/schema, Ollama, and embedding configuration"
        print(f"Embedding build failed: {message}. Completed batches are retained for resume.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Build/resume all active-catalog embeddings using the installed local model."""
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
    try:
        with connect_database(os.getenv("CATALOG_DATABASE_URL", "postgresql:///conversational_commerce"), autocommit=True) as db:
            with OllamaEmbedder(EmbeddingSettings.from_env()) as embedder:
                print(json.dumps(build_index(db, embedder), indent=2))
        return 0
    except (CatalogError, psycopg.Error, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, CatalogError) else "Check database/schema, Ollama, and embedding configuration"
        print(f"Embedding build failed: {message}. Completed batches are retained for resume.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Validate demo configuration locally, with no database, server, or inference calls."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.deployment_checks import deployment_errors
from app.operations_config import OperationsSettings
from app.providers import validate_llm
from app.qdrant_client import QdrantSettings
from app.reranker import create_reranker
from app.usage_budget import validate_budget_configuration


def main():
    try:
        settings, operations = Settings(), OperationsSettings.from_env()
        errors = deployment_errors(settings, operations)
        validate_budget_configuration()
        validate_llm(settings)
        QdrantSettings.from_env()
        if not errors:
            create_reranker()  # Hosted configuration only; no inference.
    except (ValueError, RuntimeError):
        print("Invalid provider or operational configuration; check the deployment template.")
        return 1
    for error in errors:
        print(error)
    if not errors:
        print(
            "Configuration passed. Live readiness, credentials/quota, and capacity remain unverified."
        )
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())

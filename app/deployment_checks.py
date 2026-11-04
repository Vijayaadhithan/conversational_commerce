"""Fail closed on unsafe demo configuration without contacting dependencies."""

import os


def deployment_errors(settings, operations):
    errors = []
    if not operations.auth_required:
        errors.append("API_AUTH_REQUIRED must be 1")
    if not operations.idempotency_required:
        errors.append("API_REQUIRE_IDEMPOTENCY_KEY must be 1")
    if settings.provider == "ollama":
        errors.append("Select a hosted LLM_PROVIDER for the demo server")
    if os.getenv("RERANK_PROVIDER", "local") != "voyage":
        errors.append("RERANK_PROVIDER must be voyage for the demo server")
    if operations.alert_rss_mb > 1024:
        errors.append("API_ALERT_RSS_MB must not exceed the 1024 MiB API budget")
    if operations.model_concurrency > 2:
        errors.append("Validate higher concurrency before exceeding 2 demo workloads")
    if operations.queue_size > 4 or operations.queue_timeout > 30:
        errors.append("Demo admission requires queue size <=4 and wait <=30 seconds")
    if (
        int(os.getenv("API_DAILY_PROVIDER_CALLS", "1000")) <= 0
        or int(os.getenv("API_USER_DAILY_PROVIDER_CALLS", "200")) <= 0
    ):
        errors.append("Daily deployment and user provider call limits must be enabled")
    return errors

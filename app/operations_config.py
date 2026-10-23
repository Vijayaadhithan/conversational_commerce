"""Operations config."""

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationsSettings:
    auth_required: bool = True
    idempotency_required: bool = True
    requests_per_minute: int = 120
    request_burst: int = 30
    model_requests_per_minute: int = 30
    model_burst: int = 8
    model_concurrency: int = 1
    queue_size: int = 8
    queue_timeout: float = 120
    monitor_interval: float = 30
    alert_error_ratio: float = 0.1
    alert_latency_seconds: float = 60
    alert_rss_mb: int = 8192

    @classmethod
    def from_env(cls):
        names = {
            "auth_required": "API_AUTH_REQUIRED",
            "idempotency_required": "API_REQUIRE_IDEMPOTENCY_KEY",
            "requests_per_minute": "API_REQUESTS_PER_MINUTE",
            "request_burst": "API_REQUEST_BURST",
            "model_requests_per_minute": "API_MODEL_REQUESTS_PER_MINUTE",
            "model_burst": "API_MODEL_BURST",
            "model_concurrency": "API_MODEL_CONCURRENCY",
            "queue_size": "API_MODEL_QUEUE_SIZE",
            "queue_timeout": "API_MODEL_QUEUE_TIMEOUT_SECONDS",
            "monitor_interval": "API_MONITOR_INTERVAL_SECONDS",
            "alert_error_ratio": "API_ALERT_ERROR_RATIO",
            "alert_latency_seconds": "API_ALERT_LATENCY_SECONDS",
            "alert_rss_mb": "API_ALERT_RSS_MB",
        }
        defaults = cls()
        values = {}
        for field, name in names.items():
            value = os.getenv(name)
            if value is None:
                continue
            default = getattr(defaults, field)
            if isinstance(default, bool):
                if value not in {"0", "1"}:
                    raise ValueError(f"{name} must be 0 or 1")
                values[field] = value == "1"
            else:
                values[field] = (
                    float
                    if field
                    in {
                        "queue_timeout",
                        "monitor_interval",
                        "alert_error_ratio",
                        "alert_latency_seconds",
                    }
                    else int
                )(value)
        result = cls(**values)
        if (
            not 1 <= result.model_concurrency <= 4
            or not 0 <= result.queue_size <= 128
            or not 0 < result.queue_timeout <= 600
            or not 0 <= result.monitor_interval <= 3600
            or not 0 < result.alert_error_ratio <= 1
            or not 0 < result.alert_latency_seconds <= 3600
            or not 1 <= result.alert_rss_mb <= 1048576
            or any(
                not 1 <= getattr(result, k) <= 100000
                for k in (
                    "requests_per_minute",
                    "request_burst",
                    "model_requests_per_minute",
                    "model_burst",
                )
            )
            or any(not math.isfinite(v) for v in values.values() if isinstance(v, float))
        ):
            raise ValueError("Invalid API operational limits")
        return result

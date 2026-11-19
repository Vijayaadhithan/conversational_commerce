"""Metrics."""

import json
import logging
import math
import threading
import time
from collections import Counter, deque

import psutil

log = logging.getLogger("commerce.operations")


class Metrics:
    buckets = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, float("inf"))

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.latency = Counter()
        self.duration_sum = 0
        self.durations = {
            name: Counter() for name in ("model_queue_wait", "stream_first_product", "stream_done")
        }
        self.stream_turns = Counter()
        self.recent = deque(maxlen=10000)
        self.started = time.monotonic()
        self.dependencies = {}
        self.alerts = []
        self.process = psutil.Process()

    def increment(self, name, amount=1):
        with self.lock:
            self.counts[name] += amount

    def observe_duration(self, name, elapsed):
        with self.lock:
            self._observe_duration(name, elapsed)

    def _observe_duration(self, name, elapsed):
        duration = self.durations[name]
        duration["sum"] += elapsed
        for bound in self.buckets:
            if elapsed <= bound:
                duration[bound] += 1

    def request(self, route, method, status, elapsed, *, stream=None):
        with self.lock:
            self.counts[f"http:{method}:{route}:{status}"] += 1
            self.duration_sum += elapsed
            for bound in self.buckets:
                if elapsed <= bound:
                    self.latency[bound] += 1
            if stream is not None:
                self.stream_turns[stream.outcome, stream.error_status or status] += 1
                if not stream.replayed:
                    if stream.product_seconds is not None:
                        self._observe_duration("stream_first_product", stream.product_seconds)
                    if stream.done_seconds is not None:
                        self._observe_duration("stream_done", stream.done_seconds)
                status = stream.error_status or status
            # Operational probes must not dilute workload error/latency alerts.
            if route not in {"/livez", "/health", "/ready", "/ops/metrics", "/ops/status"}:
                self.recent.append((time.monotonic(), status, elapsed))

    def resources(self):
        memory = self.process.memory_info()
        cpu = self.process.cpu_times()
        return {
            "rss_bytes": memory.rss,
            "cpu_seconds": cpu.user + cpu.system,
            "threads": self.process.num_threads(),
            "uptime_seconds": time.monotonic() - self.started,
        }

    def evaluate(self, settings, admission):
        resources = self.resources()
        with self.lock:
            samples = [r for r in self.recent if time.monotonic() - r[0] <= 300]
            latencies = sorted(r[2] for r in samples)
            error_ratio = sum(r[1] >= 500 for r in samples) / len(samples) if samples else 0
            alerts = [
                f"{name}_unavailable"
                for name, state in self.dependencies.items()
                if state["status"] == "unavailable"
            ]
            if len(samples) >= 10 and error_ratio >= settings.alert_error_ratio:
                alerts.append("high_error_ratio")
            p95 = latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else 0
            if len(samples) >= 10 and p95 >= settings.alert_latency_seconds:
                alerts.append("high_request_latency")
            if resources["rss_bytes"] >= settings.alert_rss_mb * 1024 * 1024:
                alerts.append("high_process_memory")
            if admission.waiting and admission.waiting >= settings.queue_size:
                alerts.append("model_queue_saturated")
            if sorted(alerts) != self.alerts:
                log.warning(
                    json.dumps(
                        {
                            "event": "alert_state_changed",
                            "active": sorted(alerts),
                            "cleared": sorted(set(self.alerts) - set(alerts)),
                        }
                    )
                )
            self.alerts = sorted(alerts)
            return {
                "resources": resources,
                "dependencies": self.dependencies,
                "active_alerts": self.alerts,
                "window_seconds": 300,
                "window_samples": len(samples),
                "error_ratio": error_ratio,
                "p95_seconds": p95,
                "active_model_workloads": admission.active,
                "queued_model_workloads": admission.waiting,
            }

    def prometheus(self, admission, *, reranker_fallback_calls=0):
        lines = []
        with self.lock:
            for key, value in sorted(self.counts.items()):
                if key.startswith("http:"):
                    _, method, route, status = key.split(":", 3)
                    lines.append(
                        f'commerce_http_requests_total{{method={json.dumps(method)},route={json.dumps(route)},status="{status}"}} {value}'
                    )
                else:
                    lines.append(f"commerce_{key} {value}")
            lines.append(f"commerce_reranker_fallback_total {reranker_fallback_calls}")
            for (outcome, status), count in sorted(self.stream_turns.items()):
                lines.append(
                    f'commerce_stream_turns_total{{outcome="{outcome}",status="{status}"}} {count}'
                )
            for name, duration in self.durations.items():
                metric = f"commerce_{name}_seconds"
                lines.append(f"# TYPE {metric} histogram")
                for bound in self.buckets:
                    label = "+Inf" if math.isinf(bound) else str(bound)
                    lines.append(f'{metric}_bucket{{le="{label}"}} {duration[bound]}')
                lines.append(f"{metric}_count {duration[float('inf')]}")
                lines.append(f"{metric}_sum {duration['sum']}")
            for bound in self.buckets:
                label = "+Inf" if math.isinf(bound) else str(bound)
                lines.append(
                    f'commerce_http_duration_seconds_bucket{{le="{label}"}} {self.latency[bound]}'
                )
            lines += [
                f"commerce_http_duration_seconds_count {self.latency[float('inf')]}",
                f"commerce_http_duration_seconds_sum {self.duration_sum}",
            ]
            for name, state in sorted(self.dependencies.items()):
                lines.append(
                    f'commerce_dependency_up{{component="{name}"}} {int(state["status"] == "ready")}'
                )
                lines.append(
                    f'commerce_dependency_enabled{{component="{name}"}} {int(state["status"] != "disabled")}'
                )
            for alert in self.alerts:
                lines.append(f'commerce_alert_active{{alert="{alert}"}} 1')
        lines += [f"commerce_process_{name} {value}" for name, value in self.resources().items()]
        lines += [
            f"commerce_model_active {admission.active}",
            f"commerce_model_queued {admission.waiting}",
        ]
        return "\n".join(lines) + "\n"

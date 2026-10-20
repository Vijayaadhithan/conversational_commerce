"""Single-worker API admission, bounded telemetry, and local alert evaluation."""
import asyncio
from collections import Counter, OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import math
import os
import threading
import time
from uuid import uuid4

from fastapi import HTTPException
import psutil

log = logging.getLogger('commerce.operations')
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.propagate = False


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
    alert_error_ratio: float = .1
    alert_latency_seconds: float = 60
    alert_rss_mb: int = 8192

    @classmethod
    def from_env(cls):
        names = {'auth_required': 'API_AUTH_REQUIRED', 'idempotency_required': 'API_REQUIRE_IDEMPOTENCY_KEY',
                 'requests_per_minute': 'API_REQUESTS_PER_MINUTE', 'request_burst': 'API_REQUEST_BURST',
                 'model_requests_per_minute': 'API_MODEL_REQUESTS_PER_MINUTE', 'model_burst': 'API_MODEL_BURST',
                 'model_concurrency': 'API_MODEL_CONCURRENCY', 'queue_size': 'API_MODEL_QUEUE_SIZE',
                 'queue_timeout': 'API_MODEL_QUEUE_TIMEOUT_SECONDS', 'monitor_interval': 'API_MONITOR_INTERVAL_SECONDS',
                 'alert_error_ratio': 'API_ALERT_ERROR_RATIO', 'alert_latency_seconds': 'API_ALERT_LATENCY_SECONDS',
                 'alert_rss_mb': 'API_ALERT_RSS_MB'}
        defaults = cls()
        values = {}
        for field, name in names.items():
            value = os.getenv(name)
            if value is None: continue
            default = getattr(defaults, field)
            if isinstance(default, bool):
                if value not in {'0', '1'}: raise ValueError(f'{name} must be 0 or 1')
                values[field] = value == '1'
            else:
                values[field] = (float if field in {'queue_timeout', 'monitor_interval', 'alert_error_ratio', 'alert_latency_seconds'} else int)(value)
        result = cls(**values)
        if (not 1 <= result.model_concurrency <= 4 or not 0 <= result.queue_size <= 128
                or not 0 < result.queue_timeout <= 600 or not 0 <= result.monitor_interval <= 3600
                or not 0 < result.alert_error_ratio <= 1 or not 0 < result.alert_latency_seconds <= 3600
                or not 1 <= result.alert_rss_mb <= 1048576
                or any(not 1 <= getattr(result, k) <= 100000 for k in ('requests_per_minute', 'request_burst', 'model_requests_per_minute', 'model_burst'))
                or any(not math.isfinite(v) for v in values.values() if isinstance(v, float))):
            raise ValueError('Invalid API operational limits')
        return result


class Metrics:
    buckets = (.1, .5, 1, 2, 5, 10, 30, 60, 120, 300, float('inf'))

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.latency = Counter()
        self.duration_sum = 0
        self.recent = deque(maxlen=10000)
        self.started = time.monotonic()
        self.dependencies = {}
        self.alerts = []
        self.process = psutil.Process()

    def increment(self, name, amount=1):
        with self.lock: self.counts[name] += amount

    def request(self, route, method, status, elapsed):
        with self.lock:
            self.counts[f'http:{method}:{route}:{status}'] += 1
            self.duration_sum += elapsed
            for bound in self.buckets:
                if elapsed <= bound: self.latency[bound] += 1
            # Operational probes must not dilute workload error/latency alerts.
            if route not in {'/livez', '/health', '/ready', '/ops/metrics', '/ops/status'}:
                self.recent.append((time.monotonic(), status, elapsed))

    def resources(self):
        memory = self.process.memory_info()
        cpu = self.process.cpu_times()
        return {'rss_bytes': memory.rss, 'cpu_seconds': cpu.user + cpu.system,
                'threads': self.process.num_threads(), 'uptime_seconds': time.monotonic() - self.started}

    def evaluate(self, settings, admission):
        resources = self.resources()
        with self.lock:
            samples = [r for r in self.recent if time.monotonic() - r[0] <= 300]
            latencies = sorted(r[2] for r in samples)
            error_ratio = sum(r[1] >= 500 for r in samples) / len(samples) if samples else 0
            alerts = [f'{name}_unavailable' for name, state in self.dependencies.items() if state['status'] == 'unavailable']
            if len(samples) >= 10 and error_ratio >= settings.alert_error_ratio: alerts.append('high_error_ratio')
            p95 = latencies[math.ceil(len(latencies) * .95) - 1] if latencies else 0
            if len(samples) >= 10 and p95 >= settings.alert_latency_seconds: alerts.append('high_request_latency')
            if resources['rss_bytes'] >= settings.alert_rss_mb * 1024 * 1024: alerts.append('high_process_memory')
            if admission.waiting and admission.waiting >= settings.queue_size: alerts.append('model_queue_saturated')
            if sorted(alerts) != self.alerts:
                log.warning(json.dumps({'event': 'alert_state_changed', 'active': sorted(alerts), 'cleared': sorted(set(self.alerts) - set(alerts))}))
            self.alerts = sorted(alerts)
            return {'resources': resources, 'dependencies': self.dependencies, 'active_alerts': self.alerts,
                    'window_seconds': 300, 'window_samples': len(samples), 'error_ratio': error_ratio,
                    'p95_seconds': p95, 'active_model_workloads': admission.active, 'queued_model_workloads': admission.waiting}

    def prometheus(self, admission):
        lines = []
        with self.lock:
            for key, value in sorted(self.counts.items()):
                if key.startswith('http:'):
                    _, method, route, status = key.split(':', 3)
                    lines.append(f'commerce_http_requests_total{{method={json.dumps(method)},route={json.dumps(route)},status="{status}"}} {value}')
                else:
                    lines.append(f'commerce_{key} {value}')
            for bound in self.buckets:
                label = '+Inf' if math.isinf(bound) else str(bound)
                lines.append(f'commerce_http_duration_seconds_bucket{{le="{label}"}} {self.latency[bound]}')
            lines += [f'commerce_http_duration_seconds_count {self.latency[float("inf")]}', f'commerce_http_duration_seconds_sum {self.duration_sum}']
            for name, state in sorted(self.dependencies.items()):
                lines.append(f'commerce_dependency_up{{component="{name}"}} {int(state["status"] == "ready")}')
                lines.append(f'commerce_dependency_enabled{{component="{name}"}} {int(state["status"] != "disabled")}')
            for alert in self.alerts: lines.append(f'commerce_alert_active{{alert="{alert}"}} 1')
        lines += [f'commerce_process_{name} {value}' for name, value in self.resources().items()]
        lines += [f'commerce_model_active {admission.active}', f'commerce_model_queued {admission.waiting}']
        return '\n'.join(lines) + '\n'


class TokenBuckets:
    def __init__(self):
        self.buckets = OrderedDict()

    def consume(self, key, rate, capacity):
        now = time.monotonic()
        tokens, previous = self.buckets.pop(key, (capacity, now))
        tokens = min(capacity, tokens + (now - previous) * rate / 60)
        allowed = tokens >= 1
        self.buckets[key] = (tokens - 1 if allowed else tokens, now)
        if len(self.buckets) > 4096: self.buckets.popitem(last=False)
        return 0 if allowed else max(1, math.ceil((1 - tokens) * 60 / rate))


class Admission:
    def __init__(self, settings, metrics):
        self.settings, self.metrics = settings, metrics
        self.semaphore = asyncio.Semaphore(settings.model_concurrency)
        self.active = self.waiting = 0
        self.conversations = {}

    @asynccontextmanager
    async def workload(self):
        if self.active + self.waiting >= self.settings.model_concurrency + self.settings.queue_size:
            self.metrics.increment('model_rejected_total')
            raise HTTPException(503, 'Model queue is full; retry with the same request key', headers={'Retry-After': '5'})
        started = time.monotonic()
        self.waiting += 1
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), self.settings.queue_timeout)
            except TimeoutError as exc:
                self.metrics.increment('model_queue_timeouts_total')
                raise HTTPException(503, 'Model queue wait expired; retry with the same request key', headers={'Retry-After': '5'}) from exc
        finally:
            self.waiting -= 1
        self.active += 1
        self.metrics.increment('model_workloads_total')
        self.metrics.increment('model_queue_wait_seconds_total', time.monotonic() - started)
        try:
            yield
        finally:
            self.active -= 1
            self.semaphore.release()

    @asynccontextmanager
    async def conversation(self, key):
        lock, count = self.conversations.get(key, (asyncio.Lock(), 0))
        self.conversations[key] = (lock, count + 1)
        try:
            async with lock: yield
        finally:
            _, count = self.conversations[key]
            if count == 1: self.conversations.pop(key)
            else: self.conversations[key] = (lock, count - 1)


async def finish_on_disconnect(coroutine):
    """Keep the admission slot until async inference AND blocking threads finish."""
    task = asyncio.create_task(coroutine)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not free capacity while a torch thread or upstream model is still busy.
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
            except Exception: break
        if task.done() and not task.cancelled(): task.exception()
        raise


class OperationalMiddleware:
    def __init__(self, app, settings, state):
        self.app, self.settings, self.state = app, settings, state
        self.buckets = TokenBuckets()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope, receive, send)
        started, status = time.monotonic(), 500
        request_id = uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        async def respond(code, detail, headers=()):
            await wrapped_send({'type': 'http.response.start', 'status': code, 'headers': [(b'content-type', b'application/json'), *headers]})
            await wrapped_send({'type': 'http.response.body', 'body': json.dumps({'detail': detail}).encode()})
        async def wrapped_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message['headers'] = list(message.get('headers', [])) + [(b'x-request-id', request_id.encode())]
            await send(message)
        try:
            headers = dict(scope.get('headers', []))
            public = scope['path'] in {'/livez', '/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect'}
            if self.settings.auth_required and not public:
                token = headers.get(b'authorization', b'')
                principal = self.state.store.authenticate(token[7:].decode('latin1')) if token.startswith(b'Bearer ') and len(token) <= 256 else None
                if principal is None:
                    retry = self.buckets.consume(('unauthorized',), 60, 30)
                    return await respond(429 if retry else 401, 'Invalid or missing bearer token', [(b'www-authenticate', b'Bearer'), (b'retry-after', str(retry or 1).encode())])
            else:
                principal = {'id': 'local-development', 'role': 'admin'}
            scope['state']['principal'] = principal
            if scope['path'].startswith('/ops/') and principal['role'] != 'admin':
                return await respond(403, 'Administrator access required')
            retry = self.buckets.consume(('http', principal['id']), self.settings.requests_per_minute, self.settings.request_burst)
            if retry: return await respond(429, 'Request rate limit exceeded', [(b'retry-after', str(retry).encode())])
            # Bound input before JSON decoding, including chunked requests.
            if scope['method'] in {'POST', 'PUT', 'PATCH'}:
                body = bytearray()
                while True:
                    try:
                        event = await asyncio.wait_for(receive(), timeout=15)
                    except TimeoutError:
                        return await respond(408, 'Request body timed out')
                    if event['type'] == 'http.disconnect': return
                    body.extend(event.get('body', b''))
                    if len(body) > 262144: return await respond(413, 'Request body exceeds 256 KiB')
                    if not event.get('more_body'): break
                consumed = False
                async def replay_receive():
                    nonlocal consumed
                    if not consumed:
                        consumed = True
                        return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                    return await receive()
                await self.app(scope, replay_receive, wrapped_send)
            else:
                await self.app(scope, receive, wrapped_send)
        finally:
            route = scope.get('route')
            route = getattr(route, 'path', 'unmatched')
            elapsed = time.monotonic() - started
            self.state.metrics.request(route, scope['method'] if scope['method'] in {'GET','POST','DELETE','PUT','PATCH','OPTIONS','HEAD'} else 'OTHER', status, elapsed)
            log.info(json.dumps({'event': 'request_completed', 'request_id': request_id, 'route': route,
                                 'status': status, 'seconds': round(elapsed, 4)}))

"""Real HTTP concurrent conversations against an isolated, authenticated local API.

Uses actual Ollama, PostgreSQL, reranking and optional Redis. Only the API process
started here is terminated. Shared services and the user's conversation DB are untouched.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import psutil

from app.config import ROOT, Settings
from app.conversation_store import Store


def summarize(rows):
    values = sorted(r["seconds"] for r in rows)
    successes = sorted(r["seconds"] for r in rows if 200 <= r["status"] < 300)

    def quantile(items, q):
        return items[max(0, math.ceil(len(items) * q) - 1)] if items else None

    return {
        "requests": len(rows),
        "statuses": dict(Counter(str(r["status"]) for r in rows)),
        "p50_seconds": statistics.median(values) if values else None,
        "p95_seconds": quantile(values, 0.95),
        "successful_p95_seconds": quantile(successes, 0.95),
    }


async def benchmark(args, report, output):
    with TemporaryDirectory(prefix="commerce-load-") as temporary:
        dbpath = Path(temporary) / "conversations.sqlite3"
        store = Store(dbpath)
        _, admin = store.issue_token("benchmark-admin", "admin")
        tokens = [store.issue_token(f"benchmark-{i}")[1] for i in range(max(args.concurrency))]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        namespace = "load_" + uuid4().hex[:16]
        env = {
            **os.environ,
            "DATABASE_PATH": str(dbpath),
            "API_AUTH_REQUIRED": "1",
            "API_REQUIRE_IDEMPOTENCY_KEY": "1",
            "API_REQUESTS_PER_MINUTE": "10000",
            "API_REQUEST_BURST": "10000",
            "API_MODEL_REQUESTS_PER_MINUTE": "10000",
            "API_MODEL_BURST": "10000",
            "API_MODEL_CONCURRENCY": "1",
            "API_MODEL_QUEUE_SIZE": str(args.queue_size),
            "API_MODEL_QUEUE_TIMEOUT_SECONDS": "120",
            "API_MONITOR_INTERVAL_SECONDS": "10",
            "SEARCH_WARMUP": "1",
            "SEARCH_CACHE_NAMESPACE": namespace,
        }
        if args.disable_cache:
            env["SEARCH_CACHE_REDIS_URL"] = ""
        logfile = output.with_suffix(".server.log")
        samples, rows = [], []
        started = time.perf_counter()
        with logfile.open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--workers",
                    "1",
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

            async def sample_resources():
                api = psutil.Process(process.pid)
                while process.poll() is None:
                    try:
                        shared = 0
                        for proc in psutil.process_iter(["name", "memory_info"]):
                            if (
                                "ollama" in (proc.info["name"] or "").lower()
                                and proc.info["memory_info"]
                            ):
                                shared += proc.info["memory_info"].rss
                        samples.append(
                            {
                                "elapsed": time.perf_counter() - started,
                                "api_rss_bytes": api.memory_info().rss,
                                "ollama_shared_rss_bytes": shared,
                                "system_available_bytes": psutil.virtual_memory().available,
                                "cpu_seconds": sum(api.cpu_times()[:2]),
                            }
                        )
                    except (psutil.Error, PermissionError):
                        pass
                    await asyncio.sleep(0.5)

            sampler = asyncio.create_task(sample_resources())
            try:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", timeout=300, trust_env=False
                ) as client:
                    for _ in range(600):
                        if process.poll() is not None:
                            raise RuntimeError(f"Isolated API exited; inspect {logfile}")
                        try:
                            live = await client.get("/livez", timeout=1)
                            if live.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.5)
                    else:
                        raise RuntimeError("API startup exceeded five minutes")
                    report["startup_with_warmup_seconds"] = time.perf_counter() - started
                    admin_headers = {"Authorization": "Bearer " + admin}
                    health = await client.get("/ops/status", headers=admin_headers)
                    report["initial_status"] = health.json()
                    if any(
                        report["initial_status"]["dependencies"][k]["status"] != "ready"
                        for k in ("sqlite", "models", "postgres", "reranker")
                    ):
                        raise RuntimeError("Required dependency is unavailable; see initial_status")
                    print("Isolated API ready; real-model workload starting.", flush=True)

                    async def request(
                        method, path, token, body=None, key=None, phase="workload", record=True
                    ):
                        before = time.perf_counter()
                        headers = {"Authorization": "Bearer " + token}
                        if method == "POST":
                            headers["Idempotency-Key"] = key or str(uuid4())
                        try:
                            result = await client.request(method, path, json=body, headers=headers)
                            payload = result.json() if result.content else {}
                            status = result.status_code
                        except httpx.HTTPError as exc:
                            payload, status = {"error_type": type(exc).__name__}, 0
                        elapsed = time.perf_counter() - before
                        row = {
                            "phase": phase,
                            "operation": "message" if path.endswith("/messages") else path,
                            "seconds": elapsed,
                            "status": status,
                        }
                        if "commerce" in payload:
                            commerce = payload["commerce"]
                            row["action"] = commerce.get("action")
                            row["product_count"] = len(commerce.get("products", []))
                            row["cache"] = (
                                (commerce.get("search") or {}).get("cache", {}).get("status")
                            )
                        if record:
                            rows.append(row)
                        return status, payload

                    completed = []
                    for concurrency in args.concurrency:
                        phase = f"concurrency_{concurrency}"
                        before = time.perf_counter()

                        async def conversation(index):
                            token = tokens[index]
                            code, created = await request(
                                "POST", "/conversations", token, {"mode": "commerce"}, phase=phase
                            )
                            if code != 201:
                                return
                            cid = created["id"]
                            path = f"/conversations/{cid}/messages"
                            # Some users repeat a popular request; others vary category and budget.
                            prompts = [
                                "Show me moisturizers under $30",
                                "Show me handbags under $50",
                                "Show me lipstick under $20",
                            ]
                            for text in (prompts[index % len(prompts)], "show me cheaper options"):
                                key = str(uuid4())
                                code, payload = await request(
                                    "POST", path, token, {"message": text}, key, phase
                                )
                                if code != 200:
                                    break
                                completed.append((cid, token, key, text, payload["message_id"]))

                        for _ in range(args.rounds):
                            await asyncio.gather(*(conversation(i) for i in range(concurrency)))
                        elapsed = time.perf_counter() - before
                        phase_rows = [
                            r for r in rows if r["phase"] == phase and r["operation"] == "message"
                        ]
                        report["phases"].append(
                            {
                                "phase": phase,
                                "concurrency": concurrency,
                                "elapsed_seconds": elapsed,
                                "message_throughput_per_second": sum(
                                    r["status"] == 200 for r in phase_rows
                                )
                                / elapsed,
                                **summarize(phase_rows),
                            }
                        )
                        report["requests"] = rows
                        output.write_text(json.dumps(report, indent=2))
                        print(f"{phase}: {summarize(phase_rows)}", flush=True)

                    if completed:
                        cid, token, key, text, message_id = completed[-1]
                        status, replay = await request(
                            "POST",
                            f"/conversations/{cid}/messages",
                            token,
                            {"message": text},
                            key,
                            "retry",
                        )
                        foreign = tokens[0] if token != tokens[0] else admin
                        denied, _ = await request(
                            "GET", f"/conversations/{cid}", foreign, phase="isolation"
                        )
                        report["checks"] = {
                            "replay_same_message": status == 200
                            and replay.get("message_id") == message_id,
                            "foreign_owner_denied": denied == 404,
                        }
                    # Real queued chat calls exercise overload while the shared model is occupied.
                    _, created = await request(
                        "POST", "/conversations", tokens[0], {}, phase="overload"
                    )
                    cid = created["id"]
                    path = f"/conversations/{cid}/messages"
                    key = str(uuid4())
                    prompt = {
                        "message": "Explain why leaves change color in three short sentences."
                    }
                    first = asyncio.create_task(
                        request("POST", path, tokens[0], prompt, key, "duplicate")
                    )
                    await asyncio.sleep(0.05)
                    duplicate = await request("POST", path, tokens[0], prompt, key, "duplicate")
                    await first
                    history = await client.get(
                        path, headers={"Authorization": "Bearer " + tokens[0]}
                    )
                    report.setdefault("checks", {}).update(
                        {
                            "inflight_duplicate_or_replay": duplicate[0] in (200, 409),
                            "one_pair_after_duplicate": len(history.json().get("messages", []))
                            == 2,
                        }
                    )
                    await asyncio.gather(
                        *(
                            request("POST", path, tokens[0], prompt, phase="overload")
                            for _ in range(args.queue_size + 3)
                        )
                    )
                    report["overload"] = summarize(
                        [
                            r
                            for r in rows
                            if r["phase"] == "overload" and r["operation"] == "message"
                        ]
                    )
                    report["final_status"] = (
                        await client.get("/ops/status", headers=admin_headers)
                    ).json()
                    metrics = (await client.get("/ops/metrics", headers=admin_headers)).text
                    output.with_suffix(".prom").write_text(metrics)
                    cache = {}
                    for line in metrics.splitlines():
                        if line.startswith("commerce_cache_"):
                            name, value = line.split()
                            cache[name] = float(value)
                    hits = cache.get("commerce_cache_hit_total", 0)
                    misses = cache.get("commerce_cache_miss_total", 0)
                    report["redis_cache"] = {
                        "counters": cache,
                        "hit_rate_hits_over_hits_plus_misses": hits / (hits + misses)
                        if hits + misses
                        else None,
                        "namespace_isolated": True,
                    }
                    report["requests"] = rows
            finally:
                # Popen handle identifies only the child created above; no process-name killing.
                if process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, 30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait)
                await sampler
                report["resources"] = {
                    "samples": len(samples),
                    "sampling_interval_seconds": 0.5,
                    "peak_api_rss_bytes": max((s["api_rss_bytes"] for s in samples), default=0),
                    "peak_shared_ollama_rss_bytes": max(
                        (s["ollama_shared_rss_bytes"] for s in samples), default=0
                    ),
                    "minimum_system_available_bytes": min(
                        (s["system_available_bytes"] for s in samples), default=0
                    ),
                    "note": "Process RSS is not GPU/unified-memory allocation; Ollama is shared with other clients.",
                }
                report["elapsed_seconds"] = time.perf_counter() - started
                output.write_text(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--queue-size", type=int, default=4)
    parser.add_argument("--disable-cache", action="store_true")
    args = parser.parse_args()
    if (
        not all(1 <= n <= 16 for n in args.concurrency)
        or not 1 <= args.rounds <= 20
        or not 0 <= args.queue_size <= 16
    ):
        parser.error("Concurrency must be 1-16, rounds 1-20 and queue size 0-16")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": Settings().model,
        "method": "Real HTTP and real models, isolated conversation DB and Redis namespace; no automatic load-client retries",
        "code_sha256": {
            name: hashlib.sha256((ROOT / "app" / name).read_bytes()).hexdigest()
            for name in (
                "main.py",
                "api_security.py",
                "operations.py",
                "dependencies.py",
                "search_cache.py",
            )
        },
        "model_concurrency": 1,
        "queue_size": args.queue_size,
        "rounds": args.rounds,
        "phases": [],
    }
    asyncio.run(benchmark(args, report, args.output))
    print(f"Report: {args.output}", flush=True)


if __name__ == "__main__":
    main()

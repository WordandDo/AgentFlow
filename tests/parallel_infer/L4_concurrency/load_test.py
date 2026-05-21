#!/usr/bin/env python3
"""
L4 - data-plane load tester for the sandbox HTTP server.

Bypasses the LLM entirely: spawns N coroutines that each create a
sandbox session and call a stateless tool (default ``rag:search``)
in a tight loop for the configured duration. Prints throughput +
percentile latencies + a summary of server-side backpressure
rejections at the end.

Use this to load-test the *server* without burning LLM tokens:

    python tests/parallel_infer/L4_concurrency/load_test.py \\
        --base-url http://127.0.0.1:8080 \\
        --workers 100 --duration 30 \\
        --resource-type rag --tool rag:search
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from typing import List

import httpx


async def worker_loop(
    client: httpx.AsyncClient,
    base_url: str,
    worker_id: str,
    resource_type: str,
    tool: str,
    deadline: float,
    latencies: List[float],
    errors: dict,
):
    # Each worker gets its own session id so server-side per-worker
    # serial lock isn't a bottleneck.
    sess_url = f"{base_url}/api/v1/session/create"
    exec_url = f"{base_url}/api/v1/execute"
    try:
        r = await client.post(sess_url, json={
            "worker_id": worker_id,
            "resource_type": resource_type,
        }, timeout=10.0)
        if r.status_code >= 400:
            errors[f"session_create:{r.status_code}"] = errors.get(
                f"session_create:{r.status_code}", 0) + 1
            return
    except Exception as e:
        errors[f"session_create:{type(e).__name__}"] = errors.get(
            f"session_create:{type(e).__name__}", 0) + 1
        return

    while time.monotonic() < deadline:
        t0 = time.monotonic()
        try:
            r = await client.post(exec_url, json={
                "worker_id": worker_id,
                "action": tool,
                "params": {"query": "smoke test"},
            }, timeout=30.0)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            latencies.append(elapsed_ms)
            if r.status_code >= 400:
                errors[f"execute:{r.status_code}"] = errors.get(
                    f"execute:{r.status_code}", 0) + 1
        except Exception as e:
            errors[f"execute:{type(e).__name__}"] = errors.get(
                f"execute:{type(e).__name__}", 0) + 1

    try:
        await client.post(f"{base_url}/api/v1/session/destroy", json={
            "worker_id": worker_id,
            "resource_type": resource_type,
        }, timeout=5.0)
    except Exception:
        pass


async def main_async(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + float(args.duration)
    latencies: List[float] = []
    errors: dict = {}

    limits = httpx.Limits(max_connections=args.workers * 2,
                          max_keepalive_connections=args.workers)
    async with httpx.AsyncClient(limits=limits) as client:
        workers = []
        for i in range(int(args.workers)):
            wid = f"load_{uuid.uuid4().hex[:6]}_w{i:03d}"
            workers.append(asyncio.create_task(worker_loop(
                client=client, base_url=args.base_url, worker_id=wid,
                resource_type=args.resource_type, tool=args.tool,
                deadline=deadline, latencies=latencies, errors=errors,
            )))
        t0 = time.monotonic()
        await asyncio.gather(*workers)
        elapsed = time.monotonic() - t0

    if latencies:
        sorted_lat = sorted(latencies)

        def _p(q):
            i = max(0, min(len(sorted_lat) - 1, int(q * len(sorted_lat))))
            return sorted_lat[i]

        p50 = _p(0.50)
        p95 = _p(0.95)
        p99 = _p(0.99)
        avg = statistics.mean(latencies)
    else:
        p50 = p95 = p99 = avg = float("nan")

    qps = len(latencies) / max(elapsed, 1e-6)
    print(f"\n=== load test summary ===")
    print(f"workers={args.workers}  duration={elapsed:.1f}s")
    print(f"total_requests={len(latencies)}  qps={qps:.1f}")
    print(f"latency_ms avg={avg:.1f} p50={p50:.1f} p95={p95:.1f} p99={p99:.1f}")
    if errors:
        print("errors:")
        for k, v in sorted(errors.items()):
            print(f"  {k:30s} {v}")
    else:
        print("errors: none")

    # Fetch and print server-side backpressure snapshot.
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{args.base_url}/api/v1/server/status", timeout=5.0)
            if r.status_code < 400:
                data = r.json()
                bp = (data.get("data") or {}).get("backpressure")
                print(f"\nserver backpressure snapshot:\n{json.dumps(bp, indent=2)}")
    except Exception as e:
        print(f"(could not fetch server status: {e})")

    return 0 if not errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--resource-type", default="rag")
    parser.add_argument("--tool", default="rag:search")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

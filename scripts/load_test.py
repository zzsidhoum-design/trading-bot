"""Load test for the FastAPI surface (Phase 8 hardening).

Hammers an endpoint with concurrent GET requests and reports throughput and
latency percentiles. Uses only stdlib + httpx (already a dependency).

Usage::

    python scripts/load_test.py --concurrency 50 --duration 10 \
        --path /api/v1/health --api-key <key>

Defaults target ``http://localhost:8000`` with 32 concurrent clients.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8000"
DEFAULT_PATH = "/api/v1/health"


async def _worker(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    stop: asyncio.Event,
    latencies: list[float],
    errors: list[str],
) -> None:
    while not stop.is_set():
        start = time.perf_counter()
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            latencies.append(time.perf_counter() - start)
            continue
        latencies.append(time.perf_counter() - start)


async def _run(
    base_url: str,
    path: str,
    api_key: str,
    concurrency: int,
    duration: float,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    headers = {"X-API-Key": api_key}
    stop = asyncio.Event()
    latencies: list[float] = []
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        workers = [
            asyncio.create_task(_worker(client, url, headers, stop, latencies, errors))
            for _ in range(concurrency)
        ]
        await asyncio.sleep(duration)
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)

    elapsed = duration
    count = len(latencies)
    if not latencies:
        return {"count": 0, "requests_per_second": 0.0, "errors": len(errors)}

    latencies_ms = [lat * 1000 for lat in latencies]
    latencies_ms.sort()
    p50 = latencies_ms[int(len(latencies_ms) * 0.5)]
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    return {
        "endpoint": url,
        "concurrency": concurrency,
        "duration_seconds": elapsed,
        "count": count,
        "requests_per_second": round(count / elapsed, 1),
        "avg_ms": round(statistics.mean(latencies_ms), 2),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(latencies_ms[-1], 2),
        "errors": len(errors),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_URL)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--api-key", default="change-me")
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--duration", type=float, default=10.0)
    args = parser.parse_args()

    result = asyncio.run(
        _run(
            args.base_url,
            args.path,
            args.api_key,
            args.concurrency,
            args.duration,
        )
    )
    for key, value in result.items():
        print(f"{key:<20} {value}")


if __name__ == "__main__":
    main()

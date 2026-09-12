"""Batch client helper for model-router gateway — handles 202 Retry-After with jitter.

Usage:
    python templates/batch_client.py --help
    # or import batch_requests

Handles burst like 5 videos + 10 images that serializes to 2 switches:
- 1st request takes lock, others get 202, client retries after Retry-After.
"""
import asyncio
import random
import time
import uuid
from typing import List, Dict, Any

import httpx

GATEWAY = "http://127.0.0.1:8000"
HARD_TIMEOUT_S = 240


async def single_with_retry(
    client: httpx.AsyncClient,
    path: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    request_id: str,
) -> httpx.Response:
    """POST path with 202 Retry-After loop until 200 or timeout."""
    start = time.monotonic()
    attempt = 0
    h = {**headers, "X-Request-Id": request_id}
    while True:
        attempt += 1
        r = await client.post(f"{GATEWAY}{path}", json=payload, headers=h)
        if r.status_code != 202:
            return r
        # 202 — parse Retry-After
        try:
            ra = int(r.headers.get("Retry-After", r.json().get("retry_after", 12)))
        except Exception:
            ra = 12
        ra = max(1, min(30, ra))
        jitter = random.uniform(0, 1.0)
        sleep_s = ra + jitter
        elapsed = time.monotonic() - start
        if elapsed + sleep_s >= HARD_TIMEOUT_S:
            return r  # give up, return last 202 so caller sees timeout
        await asyncio.sleep(sleep_s)
        # keep same request_id for tracing, server will coalesce same target anyway


async def batch_requests(requests: List[Dict[str, Any]], concurrency: int = 15) -> List[Dict[str, Any]]:
    """Send burst of requests concurrently, each with retry.

    Each request dict: {"path": "/v1/chat/completions", "payload": {...}, "headers": {"X-Model-Hint": "video"}}
    Returns list of {"request_id", "status", "body"} in input order.
    """
    async with httpx.AsyncClient(timeout=HARD_TIMEOUT_S + 5) as client:
        async def run_one(idx: int, req: Dict[str, Any]) -> Dict[str, Any]:
            rid = str(uuid.uuid4())[:8]
            path = req.get("path", "/v1/chat/completions")
            payload = req.get("payload", {})
            headers = req.get("headers", {})
            r = await single_with_retry(client, path, payload, headers, rid)
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:500]}
            return {"index": idx, "request_id": rid, "status": r.status_code, "headers": dict(r.headers), "body": body}

        # launch all at once to simulate "mismo ms" burst
        tasks = [run_one(i, req) for i, req in enumerate(requests)]
        results = await asyncio.gather(*tasks)
        return sorted(results, key=lambda x: x["index"])


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="model-router batch client")
    parser.add_argument("--videos", type=int, default=5, help="number of video requests")
    parser.add_argument("--images", type=int, default=10, help="number of image requests")
    parser.add_argument("--gateway", default=GATEWAY)
    args = parser.parse_args()
    GATEWAY = args.gateway

    reqs = []
    for i in range(args.videos):
        reqs.append({"path": "/v1/chat/completions", "payload": {"prompt": f"video: clip {i}", "stream": False}, "headers": {"X-Model-Hint": "video"}})
    for i in range(args.images):
        reqs.append({"path": "/v1/images/generations", "payload": {"prompt": f"image: cat {i}"}, "headers": {"X-Model-Hint": "image"}})

    results = asyncio.run(batch_requests(reqs))
    print(json.dumps(results, indent=2))
    ok = sum(1 for r in results if r["status"] == 200)
    queued = sum(1 for r in results if r["status"] == 202)
    print(f"\nDone: {ok} ok, {queued} still queued (should be 0 after retry)")

# Notes:
# - For LLM (code) use X-Model-Hint: code or no header (default code) via /v1/chat/completions
# - For media batch, use /v1 with X-Model-Hint: image/video — it serializes via 202, coalesces to 2 switches
# - /jobs/* is async Redis-backed for long jobs (poll GET /jobs/{id}); NOT for sync LLM — use /v1 for LLM

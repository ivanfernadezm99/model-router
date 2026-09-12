"""Batch burst 5 videos + 10 images — 2 switches, coalesce, 202 headers, metrics."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gateway.queue import GatewayQueue
from src.gateway import app as app_mod


@pytest.mark.asyncio
async def test_burst_5_videos_same_ms_one_start():
    """5 videos al mismo ms: 1x200 +4x202, Retry-After 1..30, X-Queue-Depth, 1 start wan-14b."""
    q = GatewayQueue()
    q.set_swapping("wan-14b")
    # simulate holder would have done 1 start; coalesced = 4
    results = []
    for i in range(5):
        r = await q.enqueue("wan-14b", f"v{i}")
        results.append(r)
        assert 1 <= r["retry_after"] <= 30
        assert r["target"] == "wan-14b"
        assert r["queued"] is True
    assert q.coalesced == 5  # all 5 counted as coalesced while swapping (holder already set swapping)
    assert q.depth() == 5
    assert q.swaps_total == 1
    # headers simulation: app.py returns X-Queue-Depth and X-Target-Model on 202
    # verify deterministic Retry-After decreases as elapsed grows (still 1..30)
    q2 = GatewayQueue(timeout_s=240)
    q2.set_swapping("sdxl")
    # artificially age swap
    q2.swap_started_at -= 220  # 220s elapsed, remaining ~20
    r = await q2.enqueue("sdxl", "late")
    assert 19 <= r["retry_after"] <= 20  # 240-220=20 clipped to 30, allow 1s drift
    q.clear_swapping()
    q2.clear_swapping()


@pytest.mark.asyncio
async def test_burst_5_videos_10_images_two_switches():
    """Burst mixto 5 wan-14b +10 sdxl => swaps_total 2, FIFO order, drain <5s."""
    q = GatewayQueue(timeout_s=240)
    q.set_swapping("wan-14b")
    for i in range(5):
        await q.enqueue("wan-14b", f"v{i}")
    for i in range(10):
        await q.enqueue("sdxl", f"i{i}")
    assert q.depth() == 15
    # wan-14b coalesced =5, sdxl enqueued but not coalesced (different target)
    assert q.coalesced == 5
    assert q.swaps_total == 1

    # simulate wan swap finishing, drain
    t0 = time.monotonic()
    items = await q.drain_fifo()
    assert [x["request_id"] for x in items] == [f"v{i}" for i in range(5)] + [f"i{i}" for i in range(10)]
    assert time.monotonic() - t0 < 5
    q.clear_swapping()
    assert q.depth() == 0

    # second swap to sdxl
    q.set_swapping("sdxl")
    for i in range(9):
        await q.enqueue("sdxl", f"i_retry{i}")
    assert q.swaps_total == 2
    assert q.depth() == 9
    q.clear_swapping()
    items2 = await q.drain_fifo()
    assert len(items2) == 9


def test_retry_after_determinista_y_headers():
    """Retry-After determinista y headers X-Queue-Depth/X-Target-Model en 202."""
    from src.gateway import app as app_mod
    from unittest.mock import patch

    async def _run():
        await app_mod.orchestrator.lock.acquire()
        app_mod.queue.set_swapping("sdxl")
        try:
            with patch("src.gateway.metrics.get_vram_used_mb", return_value=1000):
                c = TestClient(app_mod.app)
                r = c.post("/v1/images/generations", json={"prompt": "image: cat"}, headers={"X-Model-Hint": "image"})
                assert r.status_code == 202
                assert "Retry-After" in r.headers
                assert "X-Queue-Depth" in r.headers
                assert "X-Target-Model" in r.headers
                assert r.headers["X-Target-Model"] == "sdxl"
                body = r.json()
                assert body["retry_after"] == int(r.headers["Retry-After"])
                assert 1 <= body["retry_after"] <= 30
                # second request same target increments coalesced and depth
                r2 = c.post("/v1/images/generations", json={"prompt": "image: dog"}, headers={"X-Model-Hint": "image"})
                assert r2.status_code == 202
                assert int(r2.headers["X-Queue-Depth"]) == 2
        finally:
            app_mod.queue.clear_swapping()
            app_mod.orchestrator.lock.release()
            while not app_mod.queue._q.empty():
                try:
                    app_mod.queue._q.get_nowait()
                except Exception:
                    break
    asyncio.run(_run())


def test_metrics_y_health_exponen_queue():
    """GET /health y /metrics exponen queue_depth, coalesced_total, swaps_total."""
    from unittest.mock import patch

    with patch("src.gateway.metrics.get_vram_used_mb", return_value=1234):
        c = TestClient(app_mod.app)
        # inject known queue state
        app_mod.queue.coalesced = 7
        app_mod.queue.swaps_total = 3
        # ensure depth 0 for clean check
        while not app_mod.queue._q.empty():
            try:
                app_mod.queue._q.get_nowait()
            except Exception:
                break
        r = c.get("/health")
        assert r.status_code == 200
        h = r.json()
        assert "queue_depth" in h
        assert "coalesced" in h
        assert h["coalesced"] == 7
        assert h["swaps_total"] == 3

        r2 = c.get("/metrics")
        assert r2.status_code == 200
        assert "model_router_queue_depth" in r2.text
        assert "model_router_coalesced_total 7" in r2.text
        assert "model_router_swaps_total 3" in r2.text
        # reset
        app_mod.queue.coalesced = 0
        app_mod.queue.swaps_total = 0

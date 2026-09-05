"""E2E queue — code→image→video→code <180s mocked, 202 Retry-After, FIFO drain <swap+5s."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gateway.app import app as gateway_app
from src.gateway.queue import GatewayQueue
from src.orchestrator.lifecycle import Orchestrator


def _registry_mock():
    reg = MagicMock()
    specs = {
        "coder-q4-131k": {"service": "llama-code-q4.service", "port": 8082, "health_endpoint": "/health", "vram_mb": 14000, "args": ["-np", "1"]},
        "sdxl": {"service": "sdxl.service", "port": 8188, "health_endpoint": "/system_stats", "vram_mb": 6500, "args": ["--listen", "127.0.0.1"]},
    }
    reg.resolve.side_effect = lambda k: specs[k]
    reg.models = {"coder-q4-131k": specs["coder-q4-131k"], "sdxl": specs["sdxl"]}
    return reg


@pytest.mark.asyncio
async def test_e2e_code_image_video_code_under_180s():
    """Sequential code→image→video→code each <180s (mocked health instant)."""
    reg = _registry_mock()
    orch = Orchestrator(registry=reg)
    orch.active_model = "coder-q4-131k"
    orch.active_service = "llama-code-q4.service"

    seq = ["sdxl", "sdxl", "coder-q4-131k"]  # image, video (both sdxl), back to code
    order = []

    def fake_run(action, service):
        order.append(f"{action}:{service}")
        m = MagicMock(returncode=0, stdout="")
        return m

    with patch("src.orchestrator.lifecycle._run_systemctl", side_effect=fake_run):
        with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=800):
            with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 800)):
                with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                    with patch("src.orchestrator.lifecycle.poll_health", new_callable=AsyncMock, return_value=True):
                        for target in seq:
                            t0 = time.monotonic()
                            ok = await orch.switch_to(target)
                            elapsed = time.monotonic() - t0
                            assert ok is True
                            assert elapsed < 180, f"switch to {target} exceeded 180s: {elapsed}"
                        # stop-before-start: each switch (except coalesced video→video) issues stop+start
                        assert "stop:llama-code-q4.service" in order
                        assert order.count("start:sdxl.service") == 1  # video coalesces on same sdxl
                        assert "start:llama-code-q4.service" in order


@pytest.mark.asyncio
async def test_e2e_202_retry_after_during_swap():
    """Queued request while swap lock held returns 202 with Retry-After 1..30."""
    from src.gateway import app as app_mod

    async def _run():
        await app_mod.orchestrator.lock.acquire()
        app_mod.queue.set_swapping("sdxl")
        try:
            with patch("src.gateway.metrics.get_vram_used_mb", return_value=1000):
                c = TestClient(app_mod.app)
                r = c.post("/v1/images/generations", json={"prompt": "image: a cat"}, headers={"X-Model-Hint": "image"})
                assert r.status_code == 202
                assert "Retry-After" in r.headers
                ra = int(r.headers["Retry-After"])
                assert 1 <= ra <= 30
                body = r.json()
                assert body["queued"] is True
                assert body["retry_after"] == ra

                # second concurrent same-target coalesces (depth increments, no extra start)
                r2 = c.post("/v1/images/generations", json={"prompt": "image: dog"}, headers={"X-Model-Hint": "image"})
                assert r2.status_code == 202
                assert app_mod.queue.coalesced >= 1
                assert app_mod.queue.depth() == 2
        finally:
            app_mod.queue.clear_swapping()
            app_mod.orchestrator.lock.release()
            while not app_mod.queue._q.empty():
                try:
                    app_mod.queue._q.get_nowait()
                except Exception:
                    break

    await _run()


@pytest.mark.asyncio
async def test_e2e_fifo_drain_under_swap_plus_5s():
    """3 queued items drain FIFO in arrival order in < swap+5s (mocked)."""
    q = GatewayQueue(timeout_s=240)
    q.set_swapping("sdxl")
    await q.enqueue("sdxl", "a")
    await q.enqueue("coder-q4-131k", "b")
    await q.enqueue("sdxl", "c")

    swap_done = time.monotonic()
    # simulate swap finishing after 1s (mocked)
    await asyncio.sleep(0.05)
    items = await q.drain_fifo()
    drain_latency = time.monotonic() - swap_done

    assert [i["request_id"] for i in items] == ["a", "b", "c"]
    assert drain_latency < 5, f"drain took {drain_latency}s >=5s"
    assert q.depth() == 0
    q.clear_swapping()


@pytest.mark.asyncio
async def test_e2e_proxy_propagates_after_swap():
    """After successful switch, proxy streams event-stream (mock httpx)."""
    from fastapi import Request
    from src.gateway import proxy as proxy_mod

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/event-stream"}

    async def fake_aiter():
        yield b"data: ok\n\n"

    mock_resp.aiter_bytes = fake_aiter
    mock_resp.aread = AsyncMock(return_value=b"data: ok\n\n")
    mock_resp.aclose = AsyncMock()

    mock_client = AsyncMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "query_string": b"", "headers": []}

    async def receive():
        return {"type": "http.request", "body": b'{"prompt":"code: hi","stream": true}'}

    req = Request(scope, receive=receive)
    with patch("src.gateway.proxy.httpx.AsyncClient", return_value=mock_client):
        resp = await proxy_mod.proxy_request(req, 8082)
        assert resp.status_code == 200

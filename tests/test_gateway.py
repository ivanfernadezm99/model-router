import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gateway.detector import detect_task, task_to_model
from src.gateway.queue import GatewayQueue
from src.gateway.app import app


# --- detector ---

def test_detector_header_wins_over_prefix():
    body = {"prompt": "code: hello"}
    task = detect_task({"X-Model-Hint": "image"}, body)
    assert task == "image"


def test_detector_prefix_when_header_absent():
    assert detect_task({}, {"prompt": "video: animate a cat"}) == "video"
    assert detect_task({}, {"prompt": "image: a cat"}) == "image"
    assert detect_task({}, {"prompt": "code: hello world"}) == "code"


def test_detector_case_insensitive():
    assert detect_task({"x-model-hint": "CoDe"}, {}) == "code"
    assert detect_task({"X-Model-Hint": "IMAGE"}, {}) == "image"
    assert detect_task({}, {"prompt": "VIDEO: upper"}) == "video"
    assert detect_task({}, {"prompt": "CoDe: mixed"}) == "code"


def test_detector_default_code():
    assert detect_task({}, {"prompt": "hello"}) == "code"
    assert detect_task({}, {}) == "code"
    assert detect_task({}, None) == "code"
    assert detect_task({}, b'{"prompt":"hi"}') == "code"


def test_detector_unknown_hint_400():
    with pytest.raises(ValueError, match="unknown"):
        detect_task({"X-Model-Hint": "video-unknown"}, {})


def test_detector_validates_registry():
    mock_reg = MagicMock()
    mock_reg.models = {"coder-14b-100k": {}, "sdxl": {}}
    assert detect_task({"X-Model-Hint": "code"}, {}, registry=mock_reg) == "code"
    # unknown task mapping still raises
    with pytest.raises(ValueError):
        detect_task({"X-Model-Hint": "bad"}, {}, registry=mock_reg)


def test_detector_messages_content_prefix():
    body = {"messages": [{"role": "user", "content": "image: draw a dog"}]}
    assert detect_task({}, body) == "image"


def test_task_to_model():
    assert task_to_model("code") == "coder-14b-100k"
    assert task_to_model("image") == "sdxl"


# --- queue coalescing ---

@pytest.mark.asyncio
async def test_queue_same_target_coalesce():
    q = GatewayQueue()
    q.set_swapping("sdxl")
    r1 = await q.enqueue("sdxl", "id1")
    assert r1["queued"] is True
    assert r1["retry_after"] >= 1
    assert r1["target"] == "sdxl"
    r2 = await q.enqueue("sdxl", "id2")
    assert q.coalesced >= 1
    assert q.depth() == 2
    q.clear_swapping()


@pytest.mark.asyncio
async def test_queue_fifo_drain():
    q = GatewayQueue()
    await q.enqueue("sdxl", "a")
    await q.enqueue("coder-14b-100k", "b")
    await q.enqueue("sdxl", "c")
    items = await q.drain_fifo()
    assert [i["request_id"] for i in items] == ["a", "b", "c"]
    assert q.depth() == 0


@pytest.mark.asyncio
async def test_queue_retry_after_during_swap():
    q = GatewayQueue(timeout_s=240)
    q.set_swapping("sdxl")
    result = await q.enqueue("sdxl")
    assert "retry_after" in result
    assert 1 <= result["retry_after"] <= 30
    q.clear_swapping()


@pytest.mark.asyncio
async def test_queue_expiry():
    q = GatewayQueue(timeout_s=240)
    await q.enqueue("sdxl", "x")
    item = await q.dequeue()
    assert item is not None
    assert q.is_expired(item) is False
    # fake old item
    item["created_at"] = item["created_at"] - 300
    assert q.is_expired(item) is True


# --- proxy streaming mock ---

@pytest.mark.asyncio
async def test_proxy_streams_event_stream():
    from fastapi import Request
    from src.gateway import proxy as proxy_mod

    # mock httpx response that yields chunks via aiter_bytes
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/event-stream"}
    async def fake_aiter():
        yield b"data: hello\n\n"
        yield b"data: world\n\n"
    mock_resp.aiter_bytes = fake_aiter
    mock_resp.aread = AsyncMock(return_value=b"data: hello\n\n")
    mock_resp.aclose = AsyncMock()

    mock_client = AsyncMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [[b"authorization", b"Bearer tok123"], [b"content-type", b"application/json"]],
    }
    async def receive():
        return {"type": "http.request", "body": b'{"stream": true, "prompt":"hi"}'}
    req = Request(scope, receive=receive)

    with patch("src.gateway.proxy.httpx.AsyncClient", return_value=mock_client):
        resp = await proxy_mod.proxy_request(req, 8082)
        # should be StreamingResponse
        assert resp.status_code == 200
        # bearer should have been forwarded (captured in build_request)
        called_headers = mock_client.build_request.call_args[1]["headers"]
        assert called_headers.get("authorization") == "Bearer tok123" or called_headers.get("Authorization") == "Bearer tok123"


@pytest.mark.asyncio
async def test_proxy_500_passthrough():
    from fastapi import Request
    from src.gateway import proxy as proxy_mod

    mock_resp = AsyncMock()
    mock_resp.status_code = 500
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.aiter_bytes = AsyncMock()
    mock_resp.aread = AsyncMock(return_value=b'{"error":"oom"}')
    mock_resp.aclose = AsyncMock()

    mock_client = AsyncMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "query_string": b"", "headers": []}
    async def receive2():
        return {"type": "http.request", "body": b'{}'}
    req = Request(scope, receive=receive2)

    with patch("src.gateway.proxy.httpx.AsyncClient", return_value=mock_client):
        resp = await proxy_mod.proxy_request(req, 8082)
        assert resp.status_code == 500
        assert b"oom" in resp.body


def test_health_and_metrics_endpoints():
    # patch nvidia-smi to avoid host dependency
    with patch("src.gateway.metrics.get_vram_used_mb", return_value=6800):
        c = TestClient(app)
        r = c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert "model" in body
        assert "vram_used_mb" in body
        assert body["vram_used_mb"] == 6800
        assert "queue_depth" in body
        assert "status" in body

        r2 = c.get("/metrics")
        assert r2.status_code == 200
        assert "model_router_queue_depth" in r2.text
        assert "model_router_swaps_total" in r2.text


def test_v1_unknown_hint_returns_400():
    with patch("src.gateway.metrics.get_vram_used_mb", return_value=1000):
        c = TestClient(app)
        r = c.post("/v1/chat/completions", json={"prompt": "hi"}, headers={"X-Model-Hint": "bad-model"})
        assert r.status_code == 400


def test_v1_queued_when_swapping_returns_202():
    # simulate orchestrator locked
    from src.gateway import app as app_mod
    # lock it
    async def _run():
        await app_mod.orchestrator.lock.acquire()
        try:
            app_mod.queue.set_swapping("sdxl")
            c = TestClient(app_mod.app)
            r = c.post("/v1/chat/completions", json={"prompt": "image: cat"}, headers={"X-Model-Hint": "image"})
            # should be 202 because we treat locked+swapping as queued
            assert r.status_code == 202
            assert "Retry-After" in r.headers
            assert r.json().get("queued") is True
        finally:
            app_mod.queue.clear_swapping()
            app_mod.orchestrator.lock.release()
            # drain leftover
            while not app_mod.queue._q.empty():
                try:
                    app_mod.queue._q.get_nowait()
                except Exception:
                    break
    asyncio.run(_run())

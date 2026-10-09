"""Regression: warmup (/load) must fire BEFORE the loaded:true health gate.

2026-10-04 deadlock: wan-14b stayed "switching" 20min. wan_server is lazy
(loaded:false until first /generate) while poll_health requires loaded:true.
The old code POSTed /load only AFTER poll_health passed, so it never fired.
Now switch_to POSTs /load once plain HTTP 200 is up, then polls.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.health import wait_http_200
from src.orchestrator.lifecycle import Orchestrator

WAN_SPEC = {
    "service": "wan-video.service",
    "port": 8189,
    "health_endpoint": "/health",
    "vram_mb": 20000,
    "args": ["--host", "127.0.0.1", "--port", "8189"],
}


def _registry_wan():
    registry = MagicMock()
    registry.resolve.return_value = dict(WAN_SPEC)
    registry.models = {}
    return registry


def _fake_run(action, service, timeout=None):
    assert action in ("stop", "start")
    assert service.endswith(".service")
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    return m


@pytest.mark.asyncio
async def test_warmup_fires_before_loaded_gate():
    """POST /load must be issued once HTTP is up, before poll_health passes."""
    events = []

    async def fake_wait(port, endpoint, timeout_s=180):
        events.append("wait")
        return True

    async def fake_poll(port, endpoint, timeout_s=1200):
        events.append("poll")
        return True

    mock_post_resp = MagicMock(status_code=200)
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    async def fake_post(url):
        events.append("warmup")
        assert url == "http://127.0.0.1:8189/load"
        return mock_post_resp

    mock_client.post = fake_post

    orch = Orchestrator(registry=_registry_wan())
    with patch("src.orchestrator.lifecycle._run_systemctl", side_effect=_fake_run):
        with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=500):
            with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 500)):
                with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                    with patch("src.orchestrator.lifecycle.wait_port_free", new_callable=AsyncMock, return_value=True):
                        with patch("src.orchestrator.lifecycle.wait_http_200", side_effect=fake_wait):
                            with patch("src.orchestrator.lifecycle.poll_health", side_effect=fake_poll):
                                with patch("src.orchestrator.lifecycle.httpx.AsyncClient", return_value=mock_client):
                                    ok = await orch.switch_to("wan-14b")
    assert ok is True
    assert events == ["wait", "warmup", "poll"]
    assert orch.active_model == "wan-14b"


@pytest.mark.asyncio
async def test_coder_targets_skip_warmup():
    """LLM targets (llama.cpp loads at startup) must not POST /load."""
    registry = MagicMock()
    registry.resolve.return_value = {
        "service": "llama-code-q4.service",
        "port": 8082,
        "health_endpoint": "/health",
        "vram_mb": 14000,
        "args": ["-np", "1", "-c", "100000"],
    }
    registry.models = {}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    orch = Orchestrator(registry=registry)
    with patch("src.orchestrator.lifecycle._run_systemctl", side_effect=_fake_run):
        with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=500):
            with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 500)):
                with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                    with patch("src.orchestrator.lifecycle.wait_port_free", new_callable=AsyncMock, return_value=True):
                        with patch("src.orchestrator.lifecycle.poll_health", new_callable=AsyncMock, return_value=True):
                            with patch("src.orchestrator.lifecycle.props_n_ctx", new_callable=AsyncMock, return_value=100000):
                                with patch("src.orchestrator.lifecycle.httpx.AsyncClient", return_value=mock_client):
                                    ok = await orch.switch_to("coder-14b-100k")
    assert ok is True
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_wait_http_200_ignores_loaded_false():
    """200 with loaded:false counts as up (warmup comes next)."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"status": "ok", "loaded": False}
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    with patch("src.orchestrator.health.httpx.AsyncClient", return_value=mock_client):
        assert await wait_http_200(8189, "/health", timeout_s=5, interval_s=0.05) is True


@pytest.mark.asyncio
async def test_wait_http_200_times_out_on_503():
    mock_resp = MagicMock(status_code=503)
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    with patch("src.orchestrator.health.httpx.AsyncClient", return_value=mock_client):
        assert await wait_http_200(8189, "/health", timeout_s=0.3, interval_s=0.05) is False

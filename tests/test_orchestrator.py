import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# --- vram ---

from src.orchestrator import vram as vram_mod
from src.orchestrator.health import poll_health
from src.orchestrator.idle import IdleReaper
from src.orchestrator.lifecycle import Orchestrator


def _mock_run_factory(stdout="1000\n", returncode=0, side_effect=None):
    def _run(*args, **kwargs):
        if side_effect:
            raise side_effect
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        return m

    return _run


def test_vram_garbage_blocks():
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="N/A\n", returncode=0)):
        assert vram_mod.get_vram_used_mb() is None
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="ERR\n", returncode=0)):
        assert vram_mod.get_vram_used_mb() is None
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="", returncode=1)):
        assert vram_mod.get_vram_used_mb() is None
    with patch("src.orchestrator.vram.subprocess.run", side_effect=FileNotFoundError("no nvidia-smi")):
        assert vram_mod.get_vram_used_mb() is None


def test_vram_fail_closed_blocks_start():
    # None current -> can_start False
    assert vram_mod.can_start(6500, None) is False
    assert vram_mod.can_start(6500, 22528) is False  # 22GB + 6.5GB >24GB
    assert vram_mod.can_start(6500, 1000) is True
    assert vram_mod.can_start(14000, 1000) is True


def test_vram_parse_ok():
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="6800\n", returncode=0)):
        assert vram_mod.get_vram_used_mb() == 6800
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="  1500  \n", returncode=0)):
        assert vram_mod.get_vram_used_mb() == 1500


def test_vram_idle_threshold():
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="1500\n", returncode=0)):
        assert vram_mod.is_idle_vram_ok() is True
    with patch("src.orchestrator.vram.subprocess.run", return_value=MagicMock(stdout="3000\n", returncode=0)):
        assert vram_mod.is_idle_vram_ok() is False
    with patch("src.orchestrator.vram.subprocess.run", side_effect=Exception("fail")):
        assert vram_mod.is_idle_vram_ok() is False


# --- health ---


@pytest.mark.asyncio
async def test_health_success():
    mock_resp = MagicMock(status_code=200)
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    with patch("src.orchestrator.health.httpx.AsyncClient", return_value=mock_client):
        ok = await poll_health(8082, "/health", timeout_s=5, interval_s=0.05)
        assert ok is True
        mock_client.get.assert_called()


@pytest.mark.asyncio
async def test_health_timeout():
    mock_resp = MagicMock(status_code=503)
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    with patch("src.orchestrator.health.httpx.AsyncClient", return_value=mock_client):
        ok = await poll_health(8082, "/health", timeout_s=0.3, interval_s=0.05)
        assert ok is False


@pytest.mark.asyncio
async def test_health_allowlist_blocks():
    with pytest.raises(ValueError, match="allowlisted|SSRF|absolute"):
        await poll_health(8082, "http://evil.com", timeout_s=1)
    with pytest.raises(ValueError):
        await poll_health(8082, "/evil", timeout_s=1)


# --- lifecycle: lock coalescing, stop-before-start, -np, holds, shell=False ---


@pytest.mark.asyncio
async def test_lock_coalescence_5_to_1():
    """5 concurrent same-target requests -> only 1 systemctl start."""
    registry = MagicMock()
    registry.resolve.return_value = {
        "service": "sdxl.service",
        "port": 8188,
        "health_endpoint": "/system_stats",
        "vram_mb": 6500,
        "args": ["--listen", "127.0.0.1"],
    }

    orch = Orchestrator(registry=registry)
    orch.active_model = None
    orch.active_service = None

    start_calls = []

    def fake_run(action, service):
        # _run_systemctl(action, service) — shell=False enforced internally
        assert action in ("stop", "start")
        assert service.endswith(".service")
        if action == "start":
            start_calls.append(service)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    with patch("src.orchestrator.lifecycle._run_systemctl", side_effect=fake_run):
        with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=1000):
            with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 1000)):
                with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                    with patch("src.orchestrator.lifecycle.poll_health", new_callable=AsyncMock) as mock_poll:
                        mock_poll.return_value = True
                        results = await asyncio.gather(*(orch.switch_to("sdxl") for _ in range(5)))
                        # All should return True, but only one start call
                        assert all(results)
                        assert len(start_calls) == 1
                        assert start_calls[0] == "sdxl.service"


@pytest.mark.asyncio
async def test_exclusive_stop_before_start():
    registry = MagicMock()
    registry.resolve.side_effect = lambda name: {
        "coder-q4-131k": {
            "service": "llama-code-q4.service",
            "port": 8082,
            "health_endpoint": "/health",
            "vram_mb": 14000,
            "args": ["-np", "1"],
        },
        "sdxl": {
            "service": "sdxl.service",
            "port": 8188,
            "health_endpoint": "/system_stats",
            "vram_mb": 6500,
            "args": ["--listen", "127.0.0.1"],
        },
    }[name]

    orch = Orchestrator(registry=registry)
    orch.active_model = "coder-q4-131k"
    orch.active_service = "llama-code-q4.service"

    order = []

    def fake_run(action, service):
        assert action in ("stop", "start"), "shell=False — action allowlisted"
        assert service.endswith(".service")
        order.append(action + ":" + service)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    with patch("src.orchestrator.lifecycle._run_systemctl", side_effect=fake_run):
        with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=500):
            with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 500)):
                with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                    with patch("src.orchestrator.lifecycle.poll_health", new_callable=AsyncMock, return_value=True):
                        ok = await orch.switch_to("sdxl")
                        assert ok is True
                        assert order[0] == "stop:llama-code-q4.service"
                        assert order[1] == "start:sdxl.service"


@pytest.mark.asyncio
async def test_health_timeout_triggers_rollback():
    registry = MagicMock()
    registry.resolve.return_value = {
        "service": "sdxl.service",
        "port": 8188,
        "health_endpoint": "/system_stats",
        "vram_mb": 6500,
        "args": [],
    }
    orch = Orchestrator(registry=registry)

    calls = []

    def fake_run(action, service):
        calls.append(action)
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("src.orchestrator.lifecycle._run_systemctl", side_effect=fake_run):
        with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=1000):
            with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 1000)):
                with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                    with patch("src.orchestrator.lifecycle.poll_health", new_callable=AsyncMock, return_value=False):
                        ok = await orch.switch_to("sdxl")
                        assert ok is False
                        # start then stop rollback
                        assert "start" in calls
                        assert calls.count("stop") >= 1


def test_np_enforcement_blocks():
    registry = MagicMock()
    registry.resolve.return_value = {
        "service": "llama-code-q4.service",
        "port": 8082,
        "health_endpoint": "/health",
        "vram_mb": 14000,
        "args": ["-np", "2"],
    }
    orch = Orchestrator(registry=registry)
    # switch_to should raise ValueError due to -np >1
    import asyncio as _asyncio

    with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
        with pytest.raises(ValueError, match="-np"):
            _asyncio.run(orch.switch_to("coder-q4-131k"))


def test_holds_block_swap():
    registry = MagicMock()
    registry.resolve.return_value = {
        "service": "sdxl.service",
        "port": 8188,
        "health_endpoint": "/system_stats",
        "vram_mb": 6500,
        "args": [],
    }
    orch = Orchestrator(registry=registry)
    with patch("src.orchestrator.lifecycle.holds_ok", return_value=(False, True)):
        result = asyncio.run(orch.switch_to("sdxl"))
        assert result is False
    with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, False)):
        result = asyncio.run(orch.switch_to("sdxl"))
        assert result is False


# --- idle ---


@pytest.mark.asyncio
async def test_idle_reaper_fires_after_timeout():
    orch = MagicMock()
    orch.active_model = "sdxl"
    orch.stop_current = AsyncMock()

    reaper = IdleReaper(orch, idle_timeout_s=0.3, tick_s=0.1)
    reaper.start()
    # fake last_activity far in past to trigger immediately
    import time

    reaper.last_activity = time.monotonic() - 1.0
    with patch("src.orchestrator.idle.is_idle_vram_ok", return_value=True):
        await asyncio.sleep(0.35)
        assert orch.stop_current.await_count >= 1
    reaper.stop()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_idle_reset_on_activity():
    orch = MagicMock()
    orch.active_model = "sdxl"
    orch.stop_current = AsyncMock()

    reaper = IdleReaper(orch, idle_timeout_s=0.5, tick_s=0.1)
    reaper.start()
    await asyncio.sleep(0.15)
    reaper.touch()  # reset
    await asyncio.sleep(0.2)
    # should NOT have fired yet because we reset
    assert orch.stop_current.await_count == 0
    reaper.stop()
    await asyncio.sleep(0.05)


def test_service_name_injection_rejected():
    from src.orchestrator.lifecycle import _validate_service

    with pytest.raises(ValueError):
        _validate_service("foo; rm -rf /.service")
    with pytest.raises(ValueError):
        _validate_service("evil.service; echo pwned")
    _validate_service("sdxl.service")
    _validate_service("llama-code-q4.service")

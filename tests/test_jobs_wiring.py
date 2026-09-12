"""Jobs wiring to Orchestrator — /jobs shares VRAM lock with /v1."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gateway.app import app


def test_jobs_image_enqueues_and_completes_with_mocked_switch():
    """POST /jobs/image returns id and background task completes via mocked orchestrator."""
    from src.jobs.router import queue as job_queue

    # clean redis for test isolation
    try:
        job_queue.flush()
    except Exception:
        pass

    with patch("src.jobs.worker._get_orchestrator") as mock_get:
        mock_orch = MagicMock()
        mock_orch.switch_to = AsyncMock(return_value=True)
        mock_reg = MagicMock()
        mock_reg.resolve.return_value = {"service": "sdxl.service", "port": 8188}
        mock_get.return_value = (mock_orch, mock_reg)

        c = TestClient(app)
        r = c.post("/jobs/image", json={"prompt": "cat in space", "quality": "balanced"})
        assert r.status_code == 202
        job_id = r.json()["id"]
        assert len(job_id) == 32

        # allow BackgroundTasks to run (TestClient runs them sync after response)
        # poll job status
        import time

        for _ in range(10):
            r2 = c.get(f"/jobs/{job_id}")
            assert r2.status_code == 200
            body = r2.json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        body = c.get(f"/jobs/{job_id}").json()
        # with mocked switch_to True, should be completed
        assert body["status"] == "completed"
        assert body["result"]["target"] == "sdxl"
        mock_orch.switch_to.assert_called_with("sdxl")


def test_jobs_video_enqueues_and_completes():
    from src.jobs.router import queue as job_queue

    try:
        job_queue.flush()
    except Exception:
        pass

    with patch("src.jobs.worker._get_orchestrator") as mock_get:
        mock_orch = MagicMock()
        mock_orch.switch_to = AsyncMock(return_value=True)
        mock_reg = MagicMock()
        mock_reg.resolve.return_value = {"service": "wan-video.service", "port": 8189}
        mock_get.return_value = (mock_orch, mock_reg)

        c = TestClient(app)
        r = c.post("/jobs/video", json={"prompt": "animate cat"})
        assert r.status_code == 202
        job_id = r.json()["id"]
        import time

        for _ in range(10):
            body = c.get(f"/jobs/{job_id}").json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        body = c.get(f"/jobs/{job_id}").json()
        assert body["status"] == "completed"
        assert body["result"]["target"] == "wan-14b"


def test_jobs_image_quality_validation():
    c = TestClient(app)
    r = c.post("/jobs/image", json={"prompt": "cat", "quality": "ultra"})
    assert r.status_code == 400


def test_jobs_not_for_llm_documented():
    """LLM must go via /v1, not /jobs — /jobs only has image/video."""
    c = TestClient(app)
    # /jobs has no /jobs/code endpoint -> 404/405
    r = c.post("/jobs/code", json={"prompt": "hello"})
    assert r.status_code in (404, 405)
    # /v1 with default code works (mocked)
    with patch("src.gateway.metrics.get_vram_used_mb", return_value=1000):
        with patch("src.orchestrator.health.poll_health", new_callable=AsyncMock, return_value=True):
            with patch("src.orchestrator.lifecycle._run_systemctl") as mock_ctl:
                mock_ctl.return_value = MagicMock(returncode=0, stdout="")
                with patch("src.orchestrator.lifecycle.get_vram_used_mb", return_value=1000):
                    with patch("src.orchestrator.lifecycle.check_vram_for_model", return_value=(True, 1000)):
                        with patch("src.orchestrator.lifecycle.holds_ok", return_value=(True, True)):
                            with patch("src.gateway.app.proxy_request", new_callable=AsyncMock) as mock_proxy:
                                from fastapi.responses import JSONResponse

                                mock_proxy.return_value = JSONResponse({"ok": True}, status_code=200)
                                # use TestClient for /v1 LLM default
                                r = c.post("/v1/chat/completions", json={"prompt": "hello"})
                                # may be 200 or 202 depending on lock, but not 404/502
                                assert r.status_code in (200, 202)

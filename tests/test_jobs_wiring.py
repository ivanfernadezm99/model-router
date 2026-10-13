"""Jobs wiring to Orchestrator — /jobs shares VRAM lock with /v1."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gateway.app import app


def _fake_output(tmp_path):
    """Stand in for the generation step.

    The worker skips the real backend call whenever PYTEST_CURRENT_TEST is set
    (src/jobs/worker.py), so no file is ever produced and the has_file check
    marks every job failed. That is correct behaviour for a job that generated
    nothing, but it makes the "completes" path unreachable from a test.

    Injecting the file at the filesystem seam keeps the assertions honest:
    switch_to, the target routing and the status transition all still run for
    real, and only the file movement — the step that would need a GPU — is
    replaced.
    """
    produced = tmp_path / "out.bin"
    produced.write_bytes(b"x")

    def _move(job_id, payload):
        payload["file"] = str(produced)

    return _move


def _run_consumer_once(job_id: str, kind: str) -> None:
    """Drive one job through the real consumer callback.

    The RabbitMQ migration moved execution out of the request: POST /jobs/*
    only persists the job in Redis and publishes the message, and the consumer
    process runs the worker. Tests invoke the exact handler the broker calls,
    so the wiring under test is the production one.
    """
    import json

    from src.jobs.consumer import make_handler
    from src.jobs.router import queue as job_queue

    payload = (job_queue.get(job_id) or {}).get("payload", {})
    handler = make_handler(job_queue)
    channel = MagicMock()
    method = MagicMock(delivery_tag=1)
    props = MagicMock(headers={"x-attempt": 1}, message_id=job_id)
    body = json.dumps({"job_id": job_id, "kind": kind, "payload": payload}).encode()
    handler(channel, method, props, body)
    channel.basic_ack.assert_called_once_with(1)


def test_jobs_image_enqueues_and_completes_with_mocked_switch(tmp_path):
    """POST /jobs/image returns the real id; the consumer completes it."""
    from src.jobs.router import queue as job_queue

    # clean redis for test isolation
    try:
        job_queue.flush()
    except Exception:
        pass

    with patch("src.jobs.router.rabbit.publish_job", return_value=True):
        with patch("src.jobs.worker._get_orchestrator") as mock_get, patch("src.jobs.worker._move_file_to_output", side_effect=_fake_output(tmp_path)):
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
            assert job_queue.get(job_id)["status"] == "queued"

            _run_consumer_once(job_id, "image")

            body = c.get(f"/jobs/{job_id}").json()
            # with mocked switch_to True, should be completed
            assert body["status"] == "completed"
            assert body["result"]["target"] == "qwen-image"
            mock_orch.switch_to.assert_called_with("qwen-image")


def test_jobs_video_enqueues_and_completes(tmp_path):
    from src.jobs.router import queue as job_queue

    try:
        job_queue.flush()
    except Exception:
        pass

    with patch("src.jobs.router.rabbit.publish_job", return_value=True):
        with patch("src.jobs.worker._get_orchestrator") as mock_get, patch("src.jobs.worker._move_file_to_output", side_effect=_fake_output(tmp_path)):
            mock_orch = MagicMock()
            mock_orch.switch_to = AsyncMock(return_value=True)
            mock_reg = MagicMock()
            mock_reg.resolve.return_value = {"service": "wan-video.service", "port": 8189}
            mock_get.return_value = (mock_orch, mock_reg)

            c = TestClient(app)
            r = c.post("/jobs/video", json={"prompt": "animate cat"})
            assert r.status_code == 202
            job_id = r.json()["id"]

            _run_consumer_once(job_id, "video")

            body = c.get(f"/jobs/{job_id}").json()
            assert body["status"] == "completed"
            assert body["result"]["target"] == "wan-14b"


def test_jobs_post_503_when_broker_down():
    """Broker caído: 503 honesto, nunca un 202 falso ni un id fantasma."""
    from src.jobs.router import queue as job_queue

    try:
        job_queue.flush()
    except Exception:
        pass

    with patch("src.jobs.router.rabbit.publish_job", return_value=False):
        c = TestClient(app)
        r = c.post("/jobs/image", json={"prompt": "cat", "quality": "draft"})
    assert r.status_code == 503
    jobs = c.get("/jobs").json()["jobs"]
    assert jobs and jobs[0]["status"] == "failed"
    assert "broker" in (jobs[0]["error"] or "")



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
        with patch("src.orchestrator.lifecycle.poll_health", new_callable=AsyncMock, return_value=True):
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


def test_get_orchestrator_prefers_served_main_instance():
    """Regresión 2026-10-07: doble import (__main__ vs src.gateway.app) daba dos
    orchestrators — /health decía coder y /jobs/models/list qwen-image."""
    import sys
    from unittest.mock import MagicMock
    from src.jobs import worker as worker_mod
    fake_main = MagicMock()
    fake_main.orchestrator = MagicMock()
    fake_main.registry = MagicMock()
    real_main = sys.modules.get("__main__")
    sys.modules["__main__"] = fake_main
    try:
        orch, reg = worker_mod._get_orchestrator()
        assert orch is fake_main.orchestrator
        assert reg is fake_main.registry
    finally:
        if real_main is not None:
            sys.modules["__main__"] = real_main


def test_get_orchestrator_falls_back_without_main_attrs():
    import sys
    from unittest.mock import MagicMock
    from src.jobs import worker as worker_mod
    from src.gateway import app as app_mod
    real_main = sys.modules.get("__main__")
    sys.modules["__main__"] = MagicMock(spec=[])  # sin orchestrator/registry
    try:
        orch, reg = worker_mod._get_orchestrator()
        assert orch is app_mod.orchestrator
        assert reg is app_mod.registry
    finally:
        if real_main is not None:
            sys.modules["__main__"] = real_main

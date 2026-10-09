"""Jobs lifecycle: no zombies, stale reap on read, cancel endpoint.

2026-10-05: un job quedo "running"/50% para siempre (switch a sdxl mato
wan-video 1s despues del submit; el worker nunca actualizo el estado).
- El worker marca failed hasta cuando lo cancelan a mitad de vuelo.
- Leer un job activo sin progreso por >45min lo marca failed (stale reap).
- DELETE /jobs/{id} cancela; el worker no revive ni pisa cancelados.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gateway.app import app
from src.jobs.router import queue as job_queue


def _flush():
    try:
        job_queue.flush()
    except Exception:
        pass


def _backdate(job_id: str, minutes: int) -> None:
    raw = job_queue.redis.get(job_queue._key(job_id))
    job = json.loads(raw)
    job["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    job_queue.redis.setex(job_queue._key(job_id), 7 * 24 * 3600, json.dumps(job))


def test_stale_running_reaped_on_read():
    """running sin moverse 2h -> GET lo marca failed con mensaje claro."""
    _flush()
    job_id = job_queue.enqueue({"prompt": "x"})
    job_queue.update_status(job_id, "running")
    _backdate(job_id, 120)

    c = TestClient(app)
    body = c.get(f"/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert "sin progreso" in (body["error"] or "")


def test_fresh_running_not_reaped():
    """running recientito se deja en paz."""
    _flush()
    job_id = job_queue.enqueue({"prompt": "x"})
    job_queue.update_status(job_id, "running")

    c = TestClient(app)
    body = c.get(f"/jobs/{job_id}").json()
    assert body["status"] == "running"


def test_stale_queued_reaped_in_list():
    _flush()
    job_id = job_queue.enqueue({"prompt": "x"})
    _backdate(job_id, 120)

    c = TestClient(app)
    jobs = c.get("/jobs").json()["jobs"]
    mine = [j for j in jobs if j["id"] == job_id][0]
    assert mine["status"] == "failed"


def test_cancel_queued_job():
    _flush()
    job_id = job_queue.enqueue({"prompt": "x"})

    c = TestClient(app)
    r = c.delete(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "failed"

    body = c.get(f"/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"] == "cancelado por el usuario"

    # cancelar de nuevo: ya termino
    r2 = c.delete(f"/jobs/{job_id}")
    assert r2.json()["detail"] == "ya termino"

    assert c.delete("/jobs/noexiste123").status_code == 404


@pytest.mark.asyncio
async def test_worker_cancelled_marks_failed():
    """CancelledError a mitad de vuelo -> failed (nunca zombie) + re-raise."""
    from src.jobs.worker import _run_job_async

    _flush()
    job_id = job_queue.enqueue({"prompt": "x"})
    with patch("src.jobs.worker._get_orchestrator") as mock_get:
        mock_orch = MagicMock()
        mock_orch.switch_to = AsyncMock(side_effect=asyncio.CancelledError())
        mock_reg = MagicMock()
        mock_reg.resolve.return_value = {"service": "wan-video.service", "port": 8189}
        mock_get.return_value = (mock_orch, mock_reg)
        with pytest.raises(asyncio.CancelledError):
            await _run_job_async(job_id, "video", {"prompt": "x"}, job_queue)
    job = job_queue.get(job_id)
    assert job["status"] == "failed"
    assert "detenido" in (job["error"] or "")


@pytest.mark.asyncio
async def test_worker_skips_pre_cancelled_job():
    """Job cancelado antes de arrancar: el worker no lo revive ni switchea."""
    from src.jobs.worker import _run_job_async

    _flush()
    job_id = job_queue.enqueue({"prompt": "x"})
    job_queue.update_status(job_id, "failed", error="cancelado por el usuario")
    with patch("src.jobs.worker._get_orchestrator") as mock_get:
        mock_orch = MagicMock()
        mock_orch.switch_to = AsyncMock(return_value=True)
        mock_reg = MagicMock()
        mock_get.return_value = (mock_orch, mock_reg)
        await _run_job_async(job_id, "video", {"prompt": "x"}, job_queue)
        mock_orch.switch_to.assert_not_called()
    job = job_queue.get(job_id)
    assert job["status"] == "failed"
    assert job["error"] == "cancelado por el usuario"

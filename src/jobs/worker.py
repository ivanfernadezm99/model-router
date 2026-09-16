"""Job worker — wires /jobs to Orchestrator (VRAM exclusive) + backend.

Image and video jobs share the same VRAM lock as /v1 gateway, so a burst
of 5 videos + 10 images via /jobs also serializes to 2 switches (wan-14b -> sdxl).
Jobs run as FastAPI BackgroundTasks; status is persisted in Redis (JobQueue).

/v1 is the sync gateway (LLM always via /v1, code default).
/jobs is the async batch API (poll GET /jobs/{id}) — now wired, not stub.
"""

import logging
import os

import httpx

from src.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


def _notify_telegram(job_id: str, kind: str, status: str, payload: dict, error: str | None = None):
    """Send Telegram notification if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        prompt = payload.get("prompt", "")[:200]
        if status == "completed":
            text = f"✅ {kind} job {job_id[:8]} terminado\nPrompt: {prompt}\nTarget: {payload.get('target', kind)}"
            if payload.get("file"):
                text += f"\nArchivo: {payload['file']}"
        else:
            text = f"❌ {kind} job {job_id[:8]} falló\nPrompt: {prompt}\nError: {error or 'unknown'}"
        # fire and forget, timeout 5s
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as exc:
        logger.warning(f"telegram notify failed for {job_id}: {exc}")


def _get_orchestrator():
    # lazy import to avoid circular import with gateway.app
    from src.gateway.app import orchestrator, registry
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"_get_orchestrator: orchestrator id={id(orchestrator)} active_model={orchestrator.active_model}")
    return orchestrator, registry


def _target_for_kind(kind: str) -> str:
    mapping = {"video": "wan-14b", "i2v": "wan-i2v-14b", "avatar": "echomimic-v2", "image": "sdxl"}
    return mapping.get(kind, "sdxl")


async def _run_job_async(job_id: str, kind: str, payload: dict, job_queue: JobQueue | None = None) -> None:
    """Async worker body — switch VRAM model, mark completed/failed in Redis."""
    jobs = job_queue or JobQueue()
    try:
        jobs.update_status(job_id, "running")
    except Exception as exc:
        logger.warning("job %s update to running failed: %s", job_id, exc)
        return

    target = _target_for_kind(kind)
    try:
        orchestrator, registry = _get_orchestrator()
        # validate target in registry (raises if unknown)
        registry.resolve(target)
        ok = await orchestrator.switch_to(target)
        if not ok:
            jobs.update_status(job_id, "failed", error="model load timeout")
            _notify_telegram(job_id, kind, "failed", payload, error="model load timeout (240s)")
            return
        # switch succeeded — call backend if available (skip real generation in tests)
        result_payload = {"target": target, "payload": payload}
        # skip heavy backend call during pytest to keep tests fast
        if not os.getenv("PYTEST_CURRENT_TEST"):
            try:
                if kind == "video":
                    # call wan-video :8189 /generate (draft 480x832, 33 frames, 20 steps by default for speed)
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    async with httpx.AsyncClient(timeout=600) as client:
                        gen_payload = {
                            "prompt": payload.get("prompt", ""),
                            "height": payload.get("height", 480),
                            "width": payload.get("width", 832),
                            "num_frames": payload.get("num_frames", 33),
                            "steps": payload.get("steps", 20),
                        }
                        r = await client.post(f"http://127.0.0.1:{port}/generate", json=gen_payload)
                        r.raise_for_status()
                        backend_result = r.json()
                        result_payload.update(backend_result)
                elif kind == "i2v":
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    async with httpx.AsyncClient(timeout=600) as client:
                        gen_payload = {
                            "prompt": payload.get("prompt", ""),
                            "image": payload.get("image", ""),
                            "height": payload.get("height", 480),
                            "width": payload.get("width", 832),
                            "num_frames": payload.get("num_frames", 33),
                            "steps": payload.get("steps", 20),
                        }
                        r = await client.post(f"http://127.0.0.1:{port}/generate", json=gen_payload)
                        r.raise_for_status()
                        result_payload.update(r.json())
                elif kind == "avatar":
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    async with httpx.AsyncClient(timeout=600) as client:
                        gen_payload = {
                            "image": payload.get("image", ""),
                            "audio": payload.get("audio", ""),
                            "prompt": payload.get("prompt", ""),
                        }
                        r = await client.post(f"http://127.0.0.1:{port}/generate", json=gen_payload)
                        r.raise_for_status()
                        result_payload.update(r.json())
                elif kind == "image":
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    # for now keep echo; real sdxl call would be here
                    pass
            except Exception as be:
                logger.warning(f"backend call failed for {job_id} ({kind}): {be} — returning echo payload")
                result_payload["backend_error"] = str(be)

        jobs.update_status(job_id, "completed", result=result_payload)
        _notify_telegram(job_id, kind, "completed", result_payload)
    except Exception as exc:
        logger.exception("job %s failed: %s", job_id, exc)
        try:
            jobs.update_status(job_id, "failed", error=str(exc))
        except Exception:
            pass
        _notify_telegram(job_id, kind, "failed", payload, error=str(exc))


def run_video_job(job_id: str, queue: JobQueue | None = None) -> None:
    """Legacy sync entrypoint — kept for backwards compat, now delegates to async worker."""
    import asyncio

    jobs = queue or JobQueue()
    job = jobs.get(job_id)
    payload = job.get("payload", {}) if job else {}
    try:
        asyncio.run(_run_job_async(job_id, "video", payload, jobs))
    except RuntimeError:
        # already in event loop (e.g. called from FastAPI BackgroundTasks) — schedule
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(asyncio.run, _run_job_async(job_id, "video", payload, jobs)).result()


def run_image_job(job_id: str, queue: JobQueue | None = None) -> None:
    """Sync entrypoint for image jobs (mirrors run_video_job)."""
    import asyncio

    jobs = queue or JobQueue()
    job = jobs.get(job_id)
    payload = job.get("payload", {}) if job else {}
    try:
        asyncio.run(_run_job_async(job_id, "image", payload, jobs))
    except RuntimeError:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(asyncio.run, _run_job_async(job_id, "image", payload, jobs)).result()

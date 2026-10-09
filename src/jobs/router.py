import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse
from pydantic import BaseModel, constr
from typing import Dict, Any
import json

from src.jobs.queue import JobQueue
from src.jobs.worker import _get_orchestrator, _run_job_async
from src.gateway.adopt import reconcile_active_model

router = APIRouter(prefix="/jobs")

queue = JobQueue()

MAX_PAYLOAD_SIZE = 5 * 1024 * 1024

class SwitchRequest(BaseModel):
    model: constr(min_length=1)


LAST_SWITCH: Dict[str, Any] = {"model": None, "status": "never", "detail": "", "started_at": None, "finished_at": None}

class VideoJobRequest(BaseModel):
    prompt: constr(min_length=1)

class I2VJobRequest(BaseModel):
    prompt: constr(min_length=1)
    image: str | None = None

class AvatarJobRequest(BaseModel):
    prompt: constr(min_length=1)
    image: str | None = None
    audio: str | None = None

class OverlaySpec(BaseModel):
    slot: constr(min_length=1)
    text: constr(min_length=1)


class ChatMsg(BaseModel):
    who: constr(min_length=1) = "bot"
    text: constr(min_length=1)


OVERLAY_SLOTS = ("title", "subtitle", "contact", "brand")


class ImageJobRequest(BaseModel):
    prompt: constr(min_length=1)
    quality: str = "balanced"
    width: int | None = None
    height: int | None = None
    steps: int | None = None
    overlays: list[OverlaySpec] | None = None
    messages: list[ChatMsg] | None = None
    restyle: bool = False
    # foto base opcional (data-url o jpg/png/webp): se usa como fondo tal cual
    # + overlays encima. Sin edición por difusión (no es fiable para texto).
    photo: str | None = None

IMAGE_PRESETS = {
    "draft": {"width": 512, "height": 512, "steps": 20, "cfg": 5},
    "balanced": {"width": 768, "height": 768, "steps": 30, "cfg": 7},
    "max": {"width": 1024, "height": 1024, "steps": 50, "cfg": 7.5},
}

class JobStatusResponse(BaseModel):
    id: str
    status: str
    created_at: str
    updated_at: str
    result: Any | None = None
    error: Any | None = None


# Peor caso legitimo: switch (1200s) + generacion (1200s) + margen.
# Un job activo sin moverse por mas que esto es un zombie (worker muerto por
# restart/shutdown, o backend caido a mitad de camino como el race wan/sdxl
# del 2026-10-05). Se marca failed en lectura para que el front no gire eterno.
STALE_AFTER_SECONDS = 45 * 60


def _is_stale(job: Dict[str, Any], now: datetime | None = None) -> bool:
    if (job or {}).get("status") not in ("queued", "running"):
        return False
    try:
        ts = datetime.fromisoformat(job.get("updated_at") or "")
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    base = now or datetime.now(timezone.utc)
    return (base - ts).total_seconds() > STALE_AFTER_SECONDS


def _reap_if_stale(job: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if job is not None and _is_stale(job):
        try:
            queue.update_status(
                job["id"],
                "failed",
                error=f"sin progreso por mas de {STALE_AFTER_SECONDS // 60} min — el worker murio o el backend cayo; reenviá el job",
            )
            return queue.get(job["id"]) or job
        except Exception as exc:
            logger.warning("stale reap failed job=%s err=%s", job.get("id"), exc)
    return job

@router.post("/image", status_code=202)
async def enqueue_image_job(req: ImageJobRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    q = req.quality.lower()
    if q not in IMAGE_PRESETS:
        raise HTTPException(status_code=400, detail="quality must be draft|balanced|max")
    preset = IMAGE_PRESETS[q]
    w = req.width or preset["width"]
    h = req.height or preset["height"]
    s = req.steps or preset["steps"]
    payload = {"prompt": req.prompt, "quality": q, "width": w, "height": h, "steps": s, "cfg": preset["cfg"]}
    if req.overlays:
        clean = []
        for o in req.overlays:
            if o.slot not in OVERLAY_SLOTS:
                raise HTTPException(status_code=400, detail=f"overlay slot inválido: {o.slot} (usá {', '.join(OVERLAY_SLOTS)})")
            clean.append({"slot": o.slot, "text": o.text[:200]})
        payload["overlays"] = clean
    if req.messages:
        clean_m = []
        for m in req.messages[:12]:
            if m.who not in ("bot", "cliente"):
                raise HTTPException(status_code=400, detail="messages[].who debe ser bot|cliente")
            clean_m.append({"who": m.who, "text": m.text[:160]})
        payload["messages"] = clean_m
    if req.photo:
        if len(req.photo) > 7 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="foto muy pesada (tope 7MB)")
        payload["photo"] = req.photo
    # restyle: true = redibuja la foto "similar pero nueva" (img2img 0.65).
    # El texto sale deformado a propósito: los textos finales los pega PIL.
    if req.restyle:
        payload["restyle"] = True
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    job_id = queue.enqueue(payload)
    # wire to Orchestrator via BackgroundTasks — shares VRAM lock with /v1
    # so 5 videos +10 images via /jobs also serializes to 2 switches (wan-14b -> sdxl)
    background_tasks.add_task(_run_job_async, job_id, "image", payload, queue)
    return {"id": job_id}

@router.post("/video", status_code=202)
async def enqueue_video_job(req: VideoJobRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    payload = {"prompt": req.prompt}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    job_id = queue.enqueue(payload)
    background_tasks.add_task(_run_job_async, job_id, "video", payload, queue)
    return {"id": job_id}

@router.post("/i2v", status_code=202)
async def enqueue_i2v_job(req: I2VJobRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    payload = {"prompt": req.prompt, "image": req.image}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    job_id = queue.enqueue(payload)
    background_tasks.add_task(_run_job_async, job_id, "i2v", payload, queue)
    return {"id": job_id}

@router.post("/avatar", status_code=202)
async def enqueue_avatar_job(req: AvatarJobRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    payload = {"prompt": req.prompt, "image": req.image, "audio": req.audio}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    job_id = queue.enqueue(payload)
    background_tasks.add_task(_run_job_async, job_id, "avatar", payload, queue)
    return {"id": job_id}

@router.get("", include_in_schema=False)
@router.get("/")
async def list_jobs(limit: int = 20) -> Dict[str, Any]:
    try:
        keys = queue.redis.keys(f"{queue._key('')}*")
    except Exception:
        keys = []
    jobs = []
    for k in sorted(keys, reverse=True)[: max(1, min(limit, 50))]:
        try:
            raw = queue.redis.get(k)
            if raw:
                j = _reap_if_stale(json.loads(raw))
                progress = j.get("progress") if isinstance(j.get("progress"), (int, float)) else None
                if progress is None:
                    if j.get("status") == "completed":
                        progress = 100
                    elif j.get("status") == "running":
                        progress = 50
                    elif j.get("status") == "queued":
                        progress = 0
                    else:
                        progress = 0
                jobs.append({kk: j.get(kk) for kk in ("id", "status", "created_at", "updated_at", "error", "result")})
                jobs[-1]["progress"] = progress
        except Exception:
            continue
    # calcular ETA para jobs en cola
    running = [j for j in jobs if j["status"] == "running"]
    queued = [j for j in jobs if j["status"] == "queued"]
    avg_duration = 180  # estimado 3min por job video
    for i, j in enumerate(queued):
        pos = len(running) + i + 1
        j["eta_seconds"] = pos * avg_duration
    return {"jobs": jobs}

@router.get("/{job_id}")
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = _reap_if_stale(queue.get(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(**{k: job.get(k) for k in ("id", "status", "created_at", "updated_at", "result", "error")})


@router.delete("/{job_id}")
async def cancel_job(job_id: str, request: Request) -> Dict[str, Any]:
    """Cancela un job activo: lo marca failed para que el front lo suelte.

    El worker chequea el estado antes y despues del POST al backend, asi que
    un job cancelado encolado no se genera y uno cancelado a mitad no pisa el
    failed con un completed tardio. Lo que ya este dentro del POST bloqueante
    igual termina en el backend (se descarta el archivo).
    """
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") not in ("queued", "running"):
        return {"id": job_id, "status": job.get("status"), "detail": "ya termino"}
    client = request.client.host if request.client else "?"
    ua = request.headers.get("user-agent", "-")
    logger.warning(
        "AUDIT cancel job=%s prev_status=%s created=%s updated=%s client=%s ua=%s path=%s",
        job_id, job.get("status"), job.get("created_at"), job.get("updated_at"),
        client, ua[:120], request.url.path,
    )
    queue.update_status(job_id, "failed", error="cancelado por el usuario")
    return {"id": job_id, "status": "failed", "detail": "cancelado"}


@router.get("/{job_id}/result")
async def job_result(job_id: str):
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    file_path = (job.get("result") or {}).get("file")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, filename=os.path.basename(file_path))


@router.get("/models/list", include_in_schema=False)
async def list_models() -> Dict[str, Any]:
    orchestrator, registry = _get_orchestrator()
    logger.info(f"list_models: orchestrator id={id(orchestrator)} active_model={orchestrator.active_model}")
    models = getattr(registry, "models", {}) or {}
    out = []
    for name, spec in models.items():
        out.append(
            {
                "name": name,
                "port": spec.get("port"),
                "vram_mb": spec.get("vram_mb"),
                "service": spec.get("service"),
                "active": orchestrator.active_model == name,
            }
        )
    return {"active": orchestrator.active_model, "orchestrator_id": id(orchestrator), "models": out}


@router.post("/switch", status_code=202)
async def switch_model(req: SwitchRequest) -> Dict[str, str]:
    orchestrator, registry = _get_orchestrator()
    try:
        registry.resolve(req.model)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown model: {req.model}")
    # bloquear si ya hay un switch en curso (flag switching_to del orchestrator)
    if orchestrator.switching_to is not None:
        return {"model": req.model, "status": "busy", "detail": f"switch en curso a {orchestrator.switching_to}, esperá"}

    # Un gateway reiniciado arranca con active_model=None mientras el modelo
    # sigue corriendo. Sin reconciliar, comparar contra None da "no soy yo" y
    # esta ruta recarga un modelo que ya está cargado: ~100s de downtime por nada.
    await reconcile_active_model(orchestrator, registry)

    if orchestrator.active_model == req.model:
        LAST_SWITCH.update({"model": req.model, "status": "already_active", "detail": "ya estaba cargado"})
        return {"model": req.model, "status": "already_active"}

    LAST_SWITCH.update(
        {"model": req.model, "status": "switching", "detail": "switch en background", "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
    )

    async def _do_switch() -> None:
        try:
            ok = await orchestrator.switch_to(req.model)
            LAST_SWITCH.update(
                {
                    "status": "active" if ok else "failed",
                    "detail": "cargado OK" if ok else "falló: ver journal del servicio (systemctl --user status) o VRAM",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            LAST_SWITCH.update({"status": "failed", "detail": f"excepción: {exc}", "finished_at": datetime.now(timezone.utc).isoformat()})

    asyncio.create_task(_do_switch())
    return {"model": req.model, "status": "switching"}


@router.get("/switch/status")
async def switch_status() -> Dict[str, Any]:
    orchestrator, _ = _get_orchestrator()
    return {**LAST_SWITCH, "active": orchestrator.active_model}

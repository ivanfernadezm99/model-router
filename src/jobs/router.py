import asyncio
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
from fastapi.responses import FileResponse
from pydantic import BaseModel, constr
from typing import Dict, Any
import json

from src.jobs import rabbit
from src.jobs.queue import JobQueue, MAX_RETRIES
from src.jobs.worker import _get_orchestrator
from src.gateway.adopt import reconcile_active_model

router = APIRouter(prefix="/jobs")

queue = JobQueue()

MAX_PAYLOAD_SIZE = 5 * 1024 * 1024


def _dispatch(kind: str, payload: Dict[str, Any]) -> str:
    """Persist the job in Redis (user-visible state) and hand it to RabbitMQ.

    Redis holds status/attempts/last_error for GET /jobs; RabbitMQ does the
    actual execution. A dead broker must never produce a fake 202.
    """
    payload["kind"] = kind
    job_id = queue.enqueue(payload)
    if not job_id:
        raise HTTPException(status_code=503, detail="cola de jobs no disponible (Redis)")
    if not rabbit.publish_job(kind, job_id, payload):
        queue.mark_failed(job_id, "broker RabbitMQ no disponible")
        raise HTTPException(
            status_code=503,
            detail="broker de jobs no disponible — reintentá en unos segundos",
        )
    return job_id

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
    attempts: int | None = None
    last_error: str | None = None


# Peor caso legitimo: switch (1200s) + generacion (1200s) + margen.
# Un job activo sin moverse por mas que esto es un zombie (worker muerto por
# restart/shutdown, o backend caido a mitad de camino como el race wan/sdxl
# del 2026-10-05). Se marca failed en lectura para que el front no gire eterno.
STALE_AFTER_SECONDS = 45 * 60


def _is_stale(job: Dict[str, Any], now: datetime | None = None) -> bool:
    # "retrying" también es candidato a stale: el backoff máximo es 900s, así
    # que si sigue retrying tras STALE_AFTER_SECONDS el broker perdió el mensaje.
    if (job or {}).get("status") not in ("queued", "running", "retrying"):
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
async def enqueue_image_job(req: ImageJobRequest) -> Dict[str, Any]:
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
    if req.restyle:
        payload["restyle"] = True
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    return {"id": _dispatch("image", payload)}


@router.post("/video", status_code=202)
async def enqueue_video_job(req: VideoJobRequest) -> Dict[str, str]:
    payload = {"prompt": req.prompt}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    return {"id": _dispatch("video", payload)}


@router.post("/i2v", status_code=202)
async def enqueue_i2v_job(req: I2VJobRequest) -> Dict[str, str]:
    payload = {"prompt": req.prompt, "image": req.image}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    return {"id": _dispatch("i2v", payload)}


@router.post("/avatar", status_code=202)
async def enqueue_avatar_job(req: AvatarJobRequest) -> Dict[str, str]:
    payload = {"prompt": req.prompt, "image": req.image, "audio": req.audio}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    return {"id": _dispatch("avatar", payload)}


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
                j = json.loads(raw)
                # mismo stale reap que GET /jobs/{id}: el listado también
                # destranca zombies para que el front no los muestre activos.
                j = _reap_if_stale(j) or j
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
                # exponer attempts y last_error para visibilidad de retry
                j["attempts"] = j.get("attempts")
                j["last_error"] = j.get("last_error")
                jobs.append({kk: j.get(kk) for kk in ("id", "status", "created_at", "updated_at", "error", "result", "attempts", "last_error")})
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
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Leer un job activo sin progreso por >45min lo marca failed: sin esto el
    # front gira eterno cuando el worker murió a mitad de job.
    job = _reap_if_stale(job) or job
    # attempts/last_error: visibilidad de retry (None en jobs antiguos sin reintentos)
    attempts = job.get("attempts")
    if not isinstance(attempts, int):
        attempts = None
    last_error = job.get("last_error")
    if not isinstance(last_error, str):
        last_error = None
    return JobStatusResponse(
        **{k: job.get(k) for k in ("id", "status", "created_at", "updated_at", "result", "error")},
        attempts=attempts,
        last_error=last_error,
    )


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(job_id: str) -> Dict[str, Any]:
    """Reintentar un job fallado.

    Si el job aún no ha alcanzado MAX_RETRIES (3), vuelve a status=queued y
    reinicia el conteo de intentos; si ya alcanzó el máximo, devuelve 409 con
    un mensaje indicando que no hay más reintentos disponibles.

    La ejecución la hace el consumer RabbitMQ: acá solo se republica.
    """
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    attempts = job.get("attempts", 0)
    if attempts is not None and attempts >= MAX_RETRIES:
        raise HTTPException(
            status_code=409,
            detail=f"Máximo de reintentos alcanzado ({MAX_RETRIES}). El job está en estado failed.",
        )
    # Volver a status=queued y reiniciar attempts
    job["status"] = "queued"
    job["attempts"] = 0
    job["error"] = None
    job["last_error"] = None
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    queue.redis.setex(queue._key(job_id), 7 * 24 * 60 * 60, json.dumps(job))
    # Republicar en RabbitMQ. Ejecutar inline duplicaba el trabajo y rompía la
    # serialización por VRAM que el consumer garantiza con prefetch=1.
    payload = job.get("payload") or {}
    kind = payload.get("kind") or "image"
    if not rabbit.publish_job(kind, job_id, payload):
        queue.mark_failed(job_id, "broker RabbitMQ no disponible")
        raise HTTPException(
            status_code=503,
            detail="broker de jobs no disponible — reintentá en unos segundos",
        )
    return {"id": job_id, "status": "queued"}


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
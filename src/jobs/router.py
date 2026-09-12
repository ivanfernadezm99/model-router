from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, constr
from typing import Dict, Any
import json

from src.jobs.queue import JobQueue
from src.jobs.worker import _run_job_async

router = APIRouter(prefix="/jobs")

queue = JobQueue()

MAX_PAYLOAD_SIZE = 5 * 1024 * 1024

class VideoJobRequest(BaseModel):
    prompt: constr(min_length=1)

class ImageJobRequest(BaseModel):
    prompt: constr(min_length=1)
    quality: str = "balanced"
    width: int | None = None
    height: int | None = None
    steps: int | None = None

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

@router.get("/{job_id}")
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(**job)

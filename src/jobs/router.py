from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, constr
from typing import Dict, Any
import json

from src.jobs.queue import JobQueue

router = APIRouter(prefix="/jobs")

queue = JobQueue()

MAX_PAYLOAD_SIZE = 5 * 1024 * 1024

class VideoJobRequest(BaseModel):
    prompt: constr(min_length=1)

class JobStatusResponse(BaseModel):
    id: str
    status: str
    created_at: str
    updated_at: str
    result: Any | None = None
    error: Any | None = None

@router.post("/video", status_code=202)
async def enqueue_video_job(req: VideoJobRequest) -> Dict[str, str]:
    payload = {"prompt": req.prompt}
    if len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    job_id = queue.enqueue(payload)
    return {"id": job_id}

@router.get("/{job_id}")
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(**job)

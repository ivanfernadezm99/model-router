import uuid
import json
import redis
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Constants
JOB_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
JOB_KEY_PREFIX = "job:"  # redis key prefix

class JobQueue:
    """Minimal Redis-backed job queue for long‑running video tasks."""

    def __init__(self, redis_url: str = "redis://127.0.0.1:6379/0") -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)

    def _key(self, job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{job_id}"

    def enqueue(self, payload: Dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "payload": payload,
            "result": None,
            "error": None,
        }
        self.redis.setex(self._key(job_id), JOB_TTL_SECONDS, json.dumps(job))
        return job_id

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        data = self.redis.get(self._key(job_id))
        if data is None:
            return None
        return json.loads(data)

    def update_status(self, job_id: str, status: str, result: Any = None, error: Any = None) -> None:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"Job {job_id} not found")
        job["status"] = status
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = error
        self.redis.setex(self._key(job_id), JOB_TTL_SECONDS, json.dumps(job))

    def mark_failed(self, job_id: str, error_msg: str = "video_backend_not_configured") -> None:
        self.update_status(job_id, "failed", error=error_msg)

    def mark_completed(self, job_id: str, result: Any) -> None:
        self.update_status(job_id, "completed", result=result)

    def delete(self, job_id: str) -> None:
        self.redis.delete(self._key(job_id))

    def flush(self) -> None:
        keys = self.redis.keys(f"{JOB_KEY_PREFIX}*")
        if keys:
            self.redis.delete(*keys)

import uuid
import json
import redis
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Constants
JOB_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
JOB_KEY_PREFIX = "job:"  # redis key prefix

MAX_RETRIES = 3              # máximo de reintentos por job
RETRY_BACKOFF_SECONDS = 60  # retraso base entre reintentos (segundos)

class JobQueue:
    """Redis-backed job queue with retry support and failure visibility.

    Features:
      - Enqueue jobs (POST /jobs/*) -> 202, job_id returned.
      - Status query (GET /jobs/{id}) -> status, attempts, last_error for DLF visibility.
      - Retry logic: enqueue marks status=queued; consumer/caller can retry with backoff.
      - Dead-letter simulation: after MAX_RETRIES, status="failed" con last_error details.
      - Non-blocking: si Redis caído, publish devuelve None y el caller maneja 503.

    The JobQueue is also used by the RabbitMQ consumer (src.jobs.consumer) as the
    persistent store for job state visible to the user (attempts/last_error). Hoy el
    consumer RabbitMQ no corre por default; el JobQueue provee esa visibilidad por sí solo.
    """

    def __init__(self, redis_url: str = "redis://127.0.0.1:6379/0") -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)

    def _key(self, job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}{job_id}"

    def enqueue(self, payload: Dict[str, Any]) -> Optional[str]:
        """Encolar un job y devolver su job_id.

        Si Redis está caído, devuelve None (el caller debe traducir a 503).
        El job se crea con status="queued" y attempts=0.
        """
        try:
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
                "attempts": 0,
            }
            self.redis.setex(self._key(job_id), JOB_TTL_SECONDS, json.dumps(job))
            return job_id
        except Exception as e:
            logger = __import__("logging").getLogger(__name__)
            logger.error("No pudo encolar job en Redis: %s", e)
            return None

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Obtener estado actual de un job."""
        try:
            data = self.redis.get(self._key(job_id))
            if data is None:
                return None
            return json.loads(data)
        except Exception as e:
            logger.error("No pudo obtener job %s de Redis: %s", job_id, e)
            return None

    def update_status(self, job_id: str, status: str, result: Any = None, error: Any = None, progress: Any = None) -> None:
        """Actualizar estado de un job en Redis."""
        try:
            job = self.get(job_id)
            if job is None:
                raise KeyError(f"Job {job_id} not found")
            job["status"] = status
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            if result is not None:
                job["result"] = result
            if error is not None:
                job["error"] = error
            if progress is not None:
                job["progress"] = progress
            # Incrementar attempts si el status es "retrying" o el job falló
            if status in ("failed", "retrying") and job.get("attempts") is not None:
                job["attempts"] = job.get("attempts", 0) + 1
            elif status == "queued":
                job["attempts"] = 0
            self.redis.setex(self._key(job_id), JOB_TTL_SECONDS, json.dumps(job))
        except Exception as e:
            logger.error("No pudo actualizar status job %s: %s", job_id, e)

    def mark_failed(self, job_id: str, error_msg: str = "unknown error") -> None:
        """Marcar job como failed con mensaje de error."""
        self.update_status(job_id, "failed", error=error_msg)

    def mark_completed(self, job_id: str, result: Any) -> None:
        """Marcar job como completed con resultado."""
        self.update_status(job_id, "completed", result=result)

    def mark_retrying(self, job_id: str, attempt: int, reason: str) -> None:
        """Marca explícito status=retrying con número de intento y motivo."""
        try:
            job = self.get(job_id)
            if job is None:
                return
            job["status"] = "retrying"
            job["attempts"] = attempt
            job["last_error"] = reason
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.redis.setex(self._key(job_id), JOB_TTL_SECONDS, json.dumps(job))
        except Exception as e:
            logger.error("No pudo marcar retrying job %s: %s", job_id, e)

    def mark_next_retry(self, job_id: str) -> None:
        """Avanzar al siguiente intento: incrementa attempts y mantiene status=queued/retrying.

        Llámase después de cada fallo para llevar la cuenta de cuántos intentos se han hecho.
        """
        try:
            job = self.get(job_id)
            if job is None:
                return
            job["attempts"] = job.get("attempts", 0) + 1
            # Si ya alcanzó MAX_RETRIES, marcamos failed; sino mantenemos status actualizado
            if job["attempts"] >= MAX_RETRIES:
                job["status"] = "failed"
                job["error"] = job.get("last_error") or "máximo de reintentos alcanzado"
            self.redis.setex(self._key(job_id), JOB_TTL_SECONDS, json.dumps(job))
        except Exception as e:
            logger.error("No pudo avanzar intento job %s: %s", job_id, e)

    def delete(self, job_id: str) -> None:
        self.redis.delete(self._key(job_id))

    def flush(self) -> None:
        keys = self.redis.keys(f"{JOB_KEY_PREFIX}*")
        if keys:
            self.redis.delete(*keys)
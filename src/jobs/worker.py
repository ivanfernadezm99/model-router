"""Video job worker entrypoint.

The video backend is intentionally not wired yet. Jobs are marked failed
explicitly instead of being routed to the SDXL image backend.
"""

from src.jobs.queue import JobQueue


def run_video_job(job_id: str, queue: JobQueue | None = None) -> None:
    """Transition a queued video job to the explicit backend-not-configured state."""
    jobs = queue or JobQueue()
    jobs.update_status(job_id, "running")
    jobs.mark_failed(job_id, "video_backend_not_configured")

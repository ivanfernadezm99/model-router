"""Standalone RabbitMQ consumer for the /jobs queue.

Run:

    python3 -m src.jobs.consumer [--kinds image,video,i2v,avatar] [--prefetch 1]

One process = one GPU lane. A single channel with prefetch=1 serializes every
job type through the exclusive VRAM lock, so a burst of jobs still resolves to
a bounded number of model switches. Scale by running N processes (one per
GPU/machine): each opens its own connection and the broker load-balances.

Retry policy (the broker enforces it, not a sleep):

* the worker runs ``worker._run_job_async(job_id, kind, payload, job_queue)``
  and persists ``completed`` / ``failed`` in Redis itself;
* when a job ends ``failed`` the consumer republishes it to the delay exchange
  with a per-message TTL (60s, then 300s, then 900s). After the TTL the broker
  dead-letters it back to ``jobs.<kind>`` and it is retried;
* after ``MAX_RETRIES`` the job stays ``failed`` in Redis and the message is
  published to ``jobs.failed`` (DLQ);
* a job cancelled by the user (``cancelado por el usuario``) is NEVER retried.

There is no blocking ``time.sleep`` inside the message callback: sleeping there
starved the pika heartbeats and the broker dropped the connection.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from pika.exceptions import AMQPConnectionError

from src.jobs import rabbit
from src.jobs.queue import JobQueue
from src.jobs.worker import _run_job_async

logger = logging.getLogger("jobs.consumer")

# Marker written by router.cancel_job — a user cancellation must never be retried.
CANCELLED_MARKER = "cancelado por el usuario"

JOB_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_KINDS = tuple(rabbit.JOB_QUEUES.keys())
RECONNECT_DELAY_SECONDS = 5


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_job(job_queue: JobQueue, job_id: str, job: Dict[str, Any]) -> None:
    job_queue.redis.setex(job_queue._key(job_id), JOB_TTL_SECONDS, json.dumps(job))


def _clear_attempts(job_queue: JobQueue, job_id: str, job: Dict[str, Any]) -> None:
    """A cancelled job is terminal, not a retry in progress: drop the counter."""
    try:
        job["attempts"] = None
        job["updated_at"] = _utcnow_iso()
        _save_job(job_queue, job_id, job)
    except Exception as exc:
        logger.error("no pude limpiar attempts de %s: %s", job_id, exc)


def _handle_failure(
    job_queue: JobQueue,
    kind: str,
    job_id: str,
    payload: Dict[str, Any],
    attempt: int,
    reason: str,
) -> None:
    """Decide between another delayed attempt and the DLQ.

    ``attempt`` is the attempt that just failed (1-based).
    """
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 1
    reason = str(reason or "error desconocido")

    if attempt <= rabbit.MAX_RETRIES:
        next_attempt = attempt + 1
        delay_ms = rabbit.RETRY_DELAY_MS.get(attempt, rabbit.RETRY_DELAY_MS[rabbit.MAX_RETRIES])
        try:
            job_queue.mark_retrying(job_id, next_attempt, reason)
        except Exception as exc:
            logger.error("no pude marcar retrying el job %s: %s", job_id, exc)
        logger.warning(
            "job %s falló en el intento %s — reintento %s en %sms (%s)",
            job_id, attempt, next_attempt, delay_ms, reason[:120],
        )
        rabbit.publish_job(
            kind, job_id, payload,
            attempt=next_attempt, delay_ms=delay_ms, reason=reason,
        )
        return

    # Attempts exhausted: honest failure state + dead letter to jobs.failed.
    logger.error("job %s agotó %s intentos: %s", job_id, rabbit.MAX_RETRIES, reason[:160])
    try:
        job = job_queue.get(job_id)
        if job is not None:
            job["status"] = "failed"
            job["error"] = reason
            job["last_error"] = f"reintentos agotados ({attempt} intentos): {reason}"
            job["updated_at"] = _utcnow_iso()
            _save_job(job_queue, job_id, job)
    except Exception as exc:
        logger.error("no pude marcar failed el job %s: %s", job_id, exc)

    try:
        import pika

        channel = rabbit.connect().channel()
        body = json.dumps(
            {
                "job_id": job_id,
                "kind": kind,
                "reason": reason,
                "attempts": attempt,
                "failed_at": _utcnow_iso(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        channel.basic_publish(
            rabbit.DLX_EXCHANGE,
            routing_key=rabbit.FAILED_ROUTING_KEY,
            body=body,
            properties=pika.BasicProperties(delivery_mode=2, message_id=job_id),
        )
        channel.close()
    except Exception as exc:
        logger.error("no pude publicar el dead letter del job %s: %s", job_id, exc)


def make_handler(job_queue: JobQueue) -> Callable:
    """Build the pika message callback bound to a Redis job store."""

    def handler(ch: Any, method: Any, props: Any, body: bytes) -> None:
        try:
            raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
            msg = json.loads(raw)
        except Exception:
            logger.error("mensaje sin JSON válido — ack y descarto")
            ch.basic_ack(method.delivery_tag)
            return

        job_id = msg.get("job_id")
        kind = msg.get("kind")
        payload = msg.get("payload") or {}
        if not job_id or not kind:
            logger.error("mensaje sin job_id/kind — ack y descarto: %s", msg)
            ch.basic_ack(method.delivery_tag)
            return

        try:
            attempt = int((getattr(props, "headers", None) or {}).get("x-attempt", 1) or 1)
        except (TypeError, ValueError):
            attempt = 1

        try:
            current: Optional[Dict[str, Any]] = job_queue.get(job_id)
        except Exception as exc:
            logger.error("no pude leer el job %s de Redis: %s", job_id, exc)
            current = None

        if current is None:
            logger.warning("job %s desconocido en Redis — ack y descarto", job_id)
            ch.basic_ack(method.delivery_tag)
            return

        status = current.get("status")
        if status == "completed":
            logger.info("job %s ya completado — ack sin reintentar", job_id)
            ch.basic_ack(method.delivery_tag)
            return

        if status == "failed":
            error = str(current.get("error") or current.get("last_error") or "")
            if CANCELLED_MARKER in error:
                logger.info("job %s cancelado por el usuario — ack sin reintentar", job_id)
                _clear_attempts(job_queue, job_id, current)
            else:
                _handle_failure(job_queue, kind, job_id, payload, attempt, error or "falló sin motivo")
            ch.basic_ack(method.delivery_tag)
            return

        # queued / running / retrying -> run it
        logger.info("ejecutando job %s (kind=%s, intento %s)", job_id, kind, attempt)
        try:
            asyncio.run(_run_job_async(job_id, kind, payload, job_queue))
        except Exception as exc:
            logger.exception("el job %s explotó en el worker: %s", job_id, exc)
            _handle_failure(
                job_queue, kind, job_id, payload, attempt,
                f"{type(exc).__name__}: {exc}",
            )
            ch.basic_ack(method.delivery_tag)
            return

        try:
            after: Optional[Dict[str, Any]] = job_queue.get(job_id)
        except Exception:
            after = None

        if after is not None and after.get("status") == "failed":
            error = str(after.get("error") or after.get("last_error") or "falló sin motivo")
            if CANCELLED_MARKER in error:
                logger.info("job %s cancelado durante la ejecución — sin reintento", job_id)
                _clear_attempts(job_queue, job_id, after)
            else:
                _handle_failure(job_queue, kind, job_id, payload, attempt, error)

        ch.basic_ack(method.delivery_tag)

    return handler


def _consume_once(queue: JobQueue, kinds: list, prefetch: int) -> None:
    channel = rabbit.connect().channel()
    rabbit.declare_topology(channel)
    channel.basic_qos(prefetch_count=prefetch)
    handler = make_handler(queue)
    for kind in kinds:
        queue_name = rabbit.JOB_QUEUES[kind]
        channel.basic_consume(queue=queue_name, on_message_callback=handler, auto_ack=False)
        logger.info("consumiendo %s", queue_name)
    channel.start_consuming()


def main(argv: Optional[list] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="RabbitMQ consumer para /jobs")
    parser.add_argument(
        "--kinds",
        default=",".join(DEFAULT_KINDS),
        help="Coma-separados: image,video,i2v,avatar. Default: todos",
    )
    parser.add_argument(
        "--prefetch",
        type=int,
        default=1,
        help="Mensajes en vuelo por canal (1 = serial por GPU)",
    )
    args = parser.parse_args(argv)

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip() in rabbit.JOB_QUEUES]
    if not kinds:
        logger.error("ningún kind válido en --kinds=%s", args.kinds)
        sys.exit(1)

    def _stop(signum, frame):  # noqa: ANN001 - signal handler signature
        logger.info("señal %s recibida — cerrando consumer", signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    queue = JobQueue()
    logger.info(
        "consumer de /jobs arrancando — kinds=%s prefetch=%s broker=%s",
        kinds, args.prefetch, rabbit.broker_url(),
    )

    while True:
        try:
            _consume_once(queue, kinds, args.prefetch)
            return
        except KeyboardInterrupt:
            logger.info("consumer detenido")
            return
        except AMQPConnectionError as exc:
            logger.error("conexión con RabbitMQ perdida: %s — reintento en %ss", exc, RECONNECT_DELAY_SECONDS)
            rabbit._reset()
            time.sleep(RECONNECT_DELAY_SECONDS)
        except Exception as exc:
            logger.exception("el consumer cayó: %s — reintento en %ss", exc, RECONNECT_DELAY_SECONDS)
            rabbit._reset()
            time.sleep(RECONNECT_DELAY_SECONDS)


if __name__ == "__main__":
    main()

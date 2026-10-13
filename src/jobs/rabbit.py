"""RabbitMQ topology and publish helpers for the /jobs queue.

Topology (all durable, exchange type ``direct``, no plugins required):

    EXCHANGE        "jobs"        routing_key=<kind>  -> queue "jobs.<kind>"
    DELAY_EXCHANGE  "jobs.delay"  routing_key=<kind>  -> queue "jobs.delay.<kind>"
    DLX_EXCHANGE    "jobs.dlx"    routing_key="failed" -> queue "jobs.failed"

Retry model: the consumer republishes a failed job to ``DELAY_EXCHANGE`` with a
per-message ``expiration`` (60s / 300s / 900s). When that TTL elapses the broker
dead-letters the message back to ``EXCHANGE`` keeping the original routing key,
so it lands in ``jobs.<kind>`` again and is retried. When attempts are exhausted
the consumer publishes the job straight to ``DLX_EXCHANGE`` -> ``jobs.failed``.

No blocking sleeps live in this module: the previous implementation slept inside
a pika callback and the missed heartbeats killed the connection.

Safety: ``publish_job`` returns False (it never raises) when the broker is down,
so the router can translate that into an honest 503 instead of a fake 202.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import pika
from pika.exceptions import AMQPChannelError, AMQPConnectionError

logger = logging.getLogger("jobs.rabbit")

EXCHANGE = "jobs"
DELAY_EXCHANGE = "jobs.delay"
DLX_EXCHANGE = "jobs.dlx"

JOB_QUEUES: Dict[str, str] = {
    "image": "jobs.image",
    "video": "jobs.video",
    "i2v": "jobs.i2v",
    "avatar": "jobs.avatar",
}

FAILED_QUEUE = "jobs.failed"
FAILED_ROUTING_KEY = "failed"

MAX_RETRIES = 3
# Backoff applied BEFORE the next attempt, keyed by the attempt that just failed.
RETRY_DELAY_MS: Dict[int, int] = {1: 60_000, 2: 300_000, 3: 900_000}

CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_URL = "amqp://guest:guest@127.0.0.1:5672/"

_connection: Optional[pika.BlockingConnection] = None
_channel: Optional[pika.adapters.blocking_connection.BlockingChannel] = None
_topology_declared = False


def broker_url() -> str:
    return os.getenv("RABBITMQ_URL", DEFAULT_URL)


def connection_parameters() -> pika.ConnectionParameters:
    params = pika.URLParameters(broker_url())
    params.socket_timeout = CONNECT_TIMEOUT_SECONDS
    params.blocked_connection_timeout = 300
    return params


def connect() -> pika.BlockingConnection:
    """Open a fresh blocking connection. Raises when the broker is unreachable."""
    return pika.BlockingConnection(connection_parameters())


def delay_queue_name(kind: str) -> str:
    return f"jobs.delay.{kind}"


def _reset() -> None:
    """Drop the cached connection/channel so the next call reconnects."""
    global _connection, _channel, _topology_declared
    try:
        if _connection is not None and _connection.is_open:
            _connection.close()
    except Exception:
        pass
    _connection = None
    _channel = None
    _topology_declared = False


def _get_channel() -> pika.adapters.blocking_connection.BlockingChannel:
    global _connection, _channel, _topology_declared
    if _connection is None or _connection.is_closed:
        _connection = connect()
        _channel = None
        _topology_declared = False
    if _channel is None or _channel.is_closed:
        _channel = _connection.channel()
    return _channel


def declare_topology(ch) -> None:
    """Declare every exchange, queue and binding this pipeline needs.

    Idempotent: safe to run on an already-declared broker.
    """
    for exchange in (EXCHANGE, DELAY_EXCHANGE, DLX_EXCHANGE):
        ch.exchange_declare(exchange=exchange, exchange_type="direct", durable=True)

    for kind, queue_name in JOB_QUEUES.items():
        ch.queue_declare(
            queue=queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-dead-letter-routing-key": FAILED_ROUTING_KEY,
            },
        )
        ch.queue_bind(queue=queue_name, exchange=EXCHANGE, routing_key=kind)

        delayed = delay_queue_name(kind)
        # No queue-level TTL: the delay is per message, so retries can use
        # 60s / 300s / 900s on the same queue.
        ch.queue_declare(
            queue=delayed,
            durable=True,
            arguments={"x-dead-letter-exchange": EXCHANGE},
        )
        ch.queue_bind(queue=delayed, exchange=DELAY_EXCHANGE, routing_key=kind)

    ch.queue_declare(queue=FAILED_QUEUE, durable=True)
    ch.queue_bind(queue=FAILED_QUEUE, exchange=DLX_EXCHANGE, routing_key=FAILED_ROUTING_KEY)


def ensure_topology() -> bool:
    """Declare the topology once per live connection.

    Returns False (never raises) when the broker is unreachable.
    """
    global _topology_declared
    if _topology_declared:
        return True
    try:
        declare_topology(_get_channel())
        _topology_declared = True
        return True
    except (AMQPConnectionError, AMQPChannelError, OSError) as exc:
        logger.error("RabbitMQ topology declaration failed: %s", exc)
        _reset()
        return False


def publish_job(
    kind: str,
    job_id: str,
    payload: Dict[str, Any],
    delay_ms: Optional[int] = None,
    attempt: int = 1,
    reason: Optional[str] = None,
) -> bool:
    """Publish one job. Returns False when the broker is down; never raises."""
    try:
        if not ensure_topology():
            return False
        ch = _get_channel()

        body = json.dumps(
            {"job_id": job_id, "kind": kind, "payload": payload},
            ensure_ascii=False,
        ).encode("utf-8")

        headers: Dict[str, Any] = {"x-attempt": int(attempt)}
        if reason:
            headers["x-reason"] = str(reason)[:200]
        props = pika.BasicProperties(
            delivery_mode=2,  # persistent
            message_id=job_id,
            headers=headers,
        )

        if delay_ms is not None:
            props.expiration = str(int(delay_ms))
            ch.basic_publish(
                exchange=DELAY_EXCHANGE,
                routing_key=kind,
                body=body,
                properties=props,
            )
            logger.info(
                "RabbitMQ published job %s to %s routing_key=%s delay_ms=%s attempt=%s",
                job_id, DELAY_EXCHANGE, kind, delay_ms, attempt,
            )
        else:
            ch.basic_publish(
                exchange=EXCHANGE,
                routing_key=kind,
                body=body,
                properties=props,
            )
            logger.info(
                "RabbitMQ published job %s to %s routing_key=%s attempt=%s",
                job_id, EXCHANGE, kind, attempt,
            )
        return True
    except (AMQPConnectionError, AMQPChannelError, OSError) as exc:
        logger.error("RabbitMQ publish failed for job %s: %s", job_id, exc)
        _reset()
        return False
    except Exception as exc:
        logger.error("RabbitMQ publish unexpected error for job %s: %s", job_id, exc, exc_info=True)
        return False

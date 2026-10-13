"""RabbitMQ queue tests — real broker round-trip + retry/DLQ decision logic.

Real-broker tests skip cleanly (not fail) when RabbitMQ is not running:

    docker compose -f docker-compose.rabbitmq.yml up -d

Retry/DLQ/cancel decision logic is unit-tested with fakeredis + mocks and runs
without a broker.
"""

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.jobs import rabbit
from src.jobs.consumer import _handle_failure, make_handler
from src.jobs.queue import JobQueue


def _broker_url() -> str:
    return os.getenv("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/")


@pytest.fixture(scope="module")
def broker_conn():
    try:
        import pika

        params = pika.URLParameters(_broker_url())
        params.socket_timeout = 3
        conn = pika.BlockingConnection(params)
    except Exception as exc:
        pytest.skip(
            f"RabbitMQ no disponible en {_broker_url()} — levanta con: "
            f"docker compose -f docker-compose.rabbitmq.yml up -d ({exc})"
        )
    yield conn
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Real broker tests (skipped when the broker is down)
# ---------------------------------------------------------------------------


def test_topology_declared(broker_conn):
    """All durable queues (jobs, failed, delay) exist after declaration."""
    ch = broker_conn.channel()
    rabbit.declare_topology(ch)
    expected = (
        list(rabbit.JOB_QUEUES.values())
        + [rabbit.FAILED_QUEUE]
        + [rabbit.delay_queue_name(k) for k in rabbit.JOB_QUEUES]
    )
    for qname in expected:
        meta = ch.queue_declare(qname, passive=True)
        # pika wraps the DeclareOk frame: the payload is at meta.method
        assert meta.method.message_count >= 0, f"queue {qname} missing"
    ch.close()


def test_publish_and_consume_roundtrip(broker_conn):
    """Persistent publish with message_id=job_id arrives intact on a bound queue."""
    ch = broker_conn.channel()
    rabbit.declare_topology(ch)
    res = ch.queue_declare(queue="", exclusive=True)
    qname = res.method.queue
    ch.queue_bind(qname, rabbit.EXCHANGE, "test")

    job_id = "test-roundtrip-1"
    payload = {"prompt": "hola", "n": 42}
    rabbit.publish_job("test", job_id, payload)

    method, props, body = ch.basic_get(qname, auto_ack=True)
    assert method is not None, "no message arrived"
    msg = json.loads(body.decode("utf-8"))
    assert msg["job_id"] == job_id
    assert msg["kind"] == "test"
    assert msg["payload"] == payload
    assert props.message_id == job_id
    assert props.delivery_mode == 2  # persistent
    assert (props.headers or {}).get("x-attempt") == 1
    ch.queue_delete(qname)
    ch.close()


def test_delayed_publish_delivers_after_delay(broker_conn):
    """Retry backoff path: per-message TTL holds the message in a delay queue,
    then it re-enters via dead-lettering to exchange `jobs` with the same key."""
    ch = broker_conn.channel()
    rabbit.declare_topology(ch)
    # Test-only delay queue wired exactly like production (DLX -> jobs), plus
    # an ephemeral target bound to the jobs exchange for routing key "test".
    dq = "jobs.delay.test"
    ch.queue_declare(dq, durable=False, arguments={"x-dead-letter-exchange": rabbit.EXCHANGE})
    ch.queue_bind(dq, rabbit.DELAY_EXCHANGE, "test")
    res = ch.queue_declare(queue="", exclusive=True)
    target = res.method.queue
    ch.queue_bind(target, rabbit.EXCHANGE, "test")

    rabbit.publish_job("test", "test-delayed-1", {"x": 1}, delay_ms=500)

    m0, _, _ = ch.basic_get(target, auto_ack=True)
    assert m0 is None, "message arrived before its TTL elapsed"
    time.sleep(1.2)
    method, props, body = ch.basic_get(target, auto_ack=True)
    assert method is not None, "message never re-entered via dead-letter"
    msg = json.loads(body.decode("utf-8"))
    assert msg["job_id"] == "test-delayed-1"
    assert (props.headers or {}).get("x-attempt") == 1
    ch.queue_delete(target)
    ch.queue_delete(dq)
    ch.close()


# ---------------------------------------------------------------------------
# Retry / DLQ decision logic (no broker needed — fakeredis + mocks)
# ---------------------------------------------------------------------------


def _fake_queue() -> JobQueue:
    import fakeredis

    q = JobQueue()
    q.redis = fakeredis.FakeRedis(decode_responses=True)
    return q


def test_retry_schedules_next_attempt_with_60s_backoff():
    q = _fake_queue()
    job_id = q.enqueue({"prompt": "x"})
    with patch.object(rabbit, "publish_job") as pub:
        _handle_failure(q, "image", job_id, {"prompt": "x"}, attempt=1, reason="boom")
    pub.assert_called_once()
    kwargs = pub.call_args.kwargs
    assert kwargs["attempt"] == 2
    assert kwargs["delay_ms"] == 60_000
    assert kwargs["reason"] == "boom"
    job = q.get(job_id)
    assert job["status"] == "retrying"
    assert job["attempts"] == 2
    assert job["last_error"] == "boom"


def test_retry_delays_progress_300_then_900():
    q = _fake_queue()
    job_id = q.enqueue({})
    with patch.object(rabbit, "publish_job") as pub:
        _handle_failure(q, "video", job_id, {}, attempt=2, reason="boom2")
        _handle_failure(q, "video", job_id, {}, attempt=3, reason="boom3")
    delays = [c.kwargs["delay_ms"] for c in pub.call_args_list]
    assert delays == [300_000, 900_000]
    attempts = [c.kwargs["attempt"] for c in pub.call_args_list]
    assert attempts == [3, 4]


def test_exhausted_attempts_go_to_dlq_and_failed_status():
    q = _fake_queue()
    job_id = q.enqueue({"p": 1})
    with patch.object(rabbit, "publish_job") as pub, patch.object(rabbit, "connect") as conn_mock:
        ch_mock = MagicMock()
        conn_mock.return_value.channel.return_value = ch_mock
        _handle_failure(q, "avatar", job_id, {"p": 1}, attempt=rabbit.MAX_RETRIES + 1, reason="agotado")
    pub.assert_not_called()
    ch_mock.basic_publish.assert_called_once()
    args, kwargs = ch_mock.basic_publish.call_args
    assert args[0] == rabbit.DLX_EXCHANGE
    assert kwargs["routing_key"] == rabbit.FAILED_ROUTING_KEY
    dlq_msg = json.loads(kwargs["body"].decode("utf-8"))
    assert dlq_msg["job_id"] == job_id
    assert dlq_msg["kind"] == "avatar"
    assert dlq_msg["reason"] == "agotado"
    assert dlq_msg["attempts"] == rabbit.MAX_RETRIES + 1
    assert dlq_msg["failed_at"]
    job = q.get(job_id)
    assert job["status"] == "failed"
    assert "agotados" in job["last_error"]


def test_handler_acks_completed_job_without_retry():
    q = _fake_queue()
    job_id = q.enqueue({})
    q.update_status(job_id, "completed", result={"file": "/tmp/x"})
    handler = make_handler(q)
    ch = MagicMock()
    method = MagicMock(delivery_tag=7)
    props = MagicMock(headers={"x-attempt": 1}, message_id=job_id)
    body = json.dumps({"job_id": job_id, "kind": "image", "payload": {}}).encode()
    with patch.object(rabbit, "publish_job") as pub, patch("src.jobs.consumer._run_job_async", new=AsyncMock()):
        handler(ch, method, props, body)
    ch.basic_ack.assert_called_once_with(7)
    pub.assert_not_called()


def test_handler_retries_failed_job_and_makes_it_visible():
    q = _fake_queue()
    job_id = q.enqueue({})
    q.update_status(job_id, "failed", error="backend no devolvió archivo")
    handler = make_handler(q)
    ch = MagicMock()
    method = MagicMock(delivery_tag=1)
    props = MagicMock(headers={"x-attempt": 1}, message_id=job_id)
    body = json.dumps({"job_id": job_id, "kind": "video", "payload": {}}).encode()
    with patch.object(rabbit, "publish_job") as pub, patch("src.jobs.consumer._run_job_async", new=AsyncMock()):
        handler(ch, method, props, body)
    ch.basic_ack.assert_called_once_with(1)
    assert pub.call_args.kwargs["attempt"] == 2
    job = q.get(job_id)
    assert job["status"] == "retrying"
    assert job["attempts"] == 2
    assert job["last_error"] == "backend no devolvió archivo"


def test_handler_never_retries_user_cancelled_job():
    q = _fake_queue()
    job_id = q.enqueue({})
    q.update_status(job_id, "failed", error="cancelado por el usuario")
    handler = make_handler(q)
    ch = MagicMock()
    method = MagicMock(delivery_tag=3)
    props = MagicMock(headers={"x-attempt": 1}, message_id=job_id)
    body = json.dumps({"job_id": job_id, "kind": "image", "payload": {}}).encode()
    with patch.object(rabbit, "publish_job") as pub, patch("src.jobs.consumer._run_job_async", new=AsyncMock()):
        handler(ch, method, props, body)
    ch.basic_ack.assert_called_once_with(3)
    pub.assert_not_called()
    job = q.get(job_id)
    assert job["status"] == "failed"
    assert job.get("attempts") is None

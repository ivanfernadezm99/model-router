# Feature: RabbitMQ job queue for /jobs

## Objective
Replace the unbounded immediate `BackgroundTask` execution of `/jobs/*` with a
RabbitMQ-backed queue so multi-user bursts are serialized safely and future
GPU/machine workers can scale out. Failures must be visible: retry state in
Redis (`retrying`, attempts, last_error) and a dead-letter queue (`jobs.failed`)
with reason + timestamp.

## Problem / Why
- Current POST /jobs/* launches `_run_job_async` as a FastAPI BackgroundTask
  with no limit: N users → N concurrent jobs → VRAM lock contention, zombie
  "running" states on restart, no retry, no failure visibility.
- User decision (2026-10-12): RabbitMQ, NOT Redis-list/RQ. Multi-user +
  future extra GPUs/machines + visible failures.

## Scope (allowed edit surfaces)
- docker-compose.rabbitmq.yml
- src/jobs/rabbit.py
- src/jobs/router.py
- src/jobs/worker.py
- src/jobs/consumer.py
- requirements.txt
- tests/test_rabbit_queue.py
- odd/tasks/rabbitmq-queue.md
- docs/RABBITMQ.md

## Constraints
- Keep the current API contract: POST /jobs/{image,video,i2v,avatar} → 202 {id};
  GET /jobs/{id} keeps existing fields (adds attempts/last_error).
- Broker down → 503 with clear message. Never silent 500, never fake success.
- Reuse worker execution (`_run_job_async`) — minimal refactor, no duplication.
- No real secrets in code/compose; env vars with local dev defaults only.
- Retry: max 3 retries after initial attempt, delays 60s/300s/900s via delayed
  exchange; ack only on success; exhausted → DLQ jobs.failed.

## Topology
```
exchange jobs (direct, durable) ──┬─> jobs.image   (durable, DLX=jobs.dlx)
                                  ├─> jobs.video   (durable, DLX=jobs.dlx)
                                  ├─> jobs.i2v     (durable, DLX=jobs.dlx)
                                  └─> jobs.avatar  (durable, DLX=jobs.dlx)
exchange jobs.delay (direct, durable) ──> jobs.delay.{kind}
    (per-message TTL via x-message-ttl header; on expiry dead-letters back to
     exchange `jobs` with the same routing key → re-enters the job queue)
exchange jobs.dlx (direct, durable) ──> jobs.failed (durable DLQ)
```
Messages: persistent (delivery_mode=2), message_id=job_id, headers x-attempt /
x-reason / x-message-ttl (retries only).

Design note: the official rabbitmq:3-management image does NOT ship the
rabbitmq_delayed_message_exchange plugin (verified 2026-10-12 — container
exited with {:plugins_not_found}), so backoff uses the standard AMQP
per-message-TTL + DLX pattern instead. No plugins required.

## Checklist
- [ ] T1 — docker-compose.rabbitmq.yml: rabbitmq:3-management, 5672+15672 on
      loopback, persistent volume, healthcheck, env-var credentials with local
      defaults, delayed-message plugin enabled at boot.
- [ ] T2 — src/jobs/rabbit.py: pika connection from env, idempotent topology
      declaration, persistent publish (message_id=job_id), queue_position,
      RabbitMQUnavailable error type.
- [ ] T3 — src/jobs/worker.py minimal refactor: `_ACTIVE_STATUSES` includes
      "retrying" so redelivered messages execute; no logic duplicated.
- [ ] T4 — src/jobs/consumer.py: standalone consumer, prefetch=1 (one channel,
      all kinds → global serialization for single GPU), ack only on success,
      retry w/ backoff via delayed exchange, Redis `retrying` + reason per
      failure, DLQ publish with reason+timestamp when exhausted, cancelled
      jobs never retried, reconnect loop.
- [ ] T5 — src/jobs/router.py: POST publishes to broker (no BackgroundTask),
      503 + honest failed status when broker down, 202 {id, queue_position};
      GET /jobs/{id} exposes attempts + last_error; stale reaper covers
      "retrying".
- [ ] T6 — requirements.txt created (was missing) incl. pika==1.4.4.
- [ ] T7 — tests/test_rabbit_queue.py: real-broker round-trip + delayed
      delivery (skip cleanly when broker down), retry/DLQ/cancel logic unit
      tests with fakeredis + mocks.
- [ ] T8 — docs/RABBITMQ.md: startup, management UI, credentials policy,
      scaling workers, DLQ inspection, retry policy, troubleshooting.
- [ ] T9 — E2E verification: compose up -d healthy, management API responds,
      real publish/consume of a no-GPU test job, simulated retry observed in
      Redis/logs, DLQ message after exhausted attempts, pytest green.

## Progress / evidence
- Branch: feat/rabbitmq-jobs-queue (from fix/tool-path-resolution @ aa63219).
- pika 1.4.4 installed system-wide (gateway runs /usr/bin/python3, no venv).
- Existing model-router-redis container serves JobQueue at 127.0.0.1:6379.
- (verification results appended here as T9 runs)

## Next steps
- Run T9 verification; append observed command results below.
- Gateway service restart needed for the new router code to take effect in
  production (left to operator unless nothing is active).

---

## Fix pass (2026-10-13) — "deja rabbitmq, arregla todo"

### Why this pass (diagnóstico con evidencia)
- El gateway corriendo (PID 2100538, arrancado 2026-10-12 20:23:10) tiene código
  VIEJO en memoria: `src/jobs/router.py` fue reescrito a las 20:25:36 (redis-only)
  y NADIE reinició el gateway → servía la versión rabbit previa que devolvía
  `{"id": true}` (bool del publish) en vez del `job_id`. Contrato roto para el front.
- `consumer.py` y `rabbit.py` NO cumplen la API que exige
  `tests/test_rabbit_queue.py` (`declare_topology`, `EXCHANGE/DELAY_EXCHANGE/DLX_EXCHANGE`,
  `JOB_QUEUES`, `FAILED_QUEUE`, `FAILED_ROUTING_KEY`, `delay_queue_name`, `connect`,
  `publish_job(kind, job_id, payload, delay_ms=, attempt=, reason=)`,
  `consumer.make_handler`, `consumer._handle_failure`). `pytest` ni siquiera importa
  (argparse a nivel import).
- `consumer.py` llamaba `_run_job_async(job_type=..., payload=...)`, pero la firma real
  es `async def _run_job_async(job_id, kind, payload, job_queue=None)` → TypeError /
  coroutine sin await. Cada job fallaba → retries → DLQ (2 en `jobs.failed`).
- `consumer.py` dormía 65s dentro del callback con heartbeat 60s → el broker cerraba
  la conexión (`StreamLostError: Transport indicated EOF`, log 20:08:57) → el consumer
  murió y nunca se levantó (no existe unidad systemd para él).
- Colas MAIN (`jobs.image` etc.) tenían `x-message-ttl=60000` + DLX=`jobs` → bucle de
  dead-letter. `jobs.delay.image` tenía 9 mensajes zombie sin TTL.

### Cleanup del broker (hecho)
- Borradas: `jobs.image/video/i2v/avatar` (args incompatibles con el diseño).
- Purgadas: `jobs.delay.*` (9 zombies) y `jobs.failed` (2).
- Borrados bindings stale `jobs -> jobs.failed` (keys image/video/i2v/avatar) que
  rompían el retry (los retries volvían al DLQ en vez de a `jobs.<kind>`).

### Checklist (este pass) — COMPLETO
- [x] T10 — `src/jobs/rabbit.py`: API del test implementada (topología idempotente:
      exchanges `jobs`/`jobs.delay`/`jobs.dlx`; `jobs.<kind>` DLX=`jobs.dlx` key `failed`
      y SIN ttl de cola; `jobs.delay.<kind>` DLX=`jobs`; `jobs.failed` ← `jobs.dlx`/`failed`;
      publish con message_id=job_id, delivery_mode=2, headers x-attempt, delay por
      `expiration` per-message; devuelve False si el broker cae, nunca lanza).
- [x] T11 — `src/jobs/consumer.py`: `make_handler(queue)` + `_handle_failure(queue, kind,
      job_id, payload, attempt, reason)`; sin argparse a nivel import; sin sleeps en el
      callback; `asyncio.run(_run_job_async(job_id, kind, payload, q))`; cancelados se
      ackean sin reintentar (`attempts=None`); backoff 60/300/900s; agotado → DLX + failed.
- [x] T12 — `src/jobs/router.py`: POST /jobs/* crean el job en Redis y publican a
      RabbitMQ; broker caído → 503 + failed honesto; 202 `{id}` de 32 chars; `retry`
      republica. Regresión arreglada de paso: `_reap_if_stale` estaba definido pero NUNCA
      se llamaba (la reescritura anterior lo desconectó) → restaurado en `GET /jobs/{id}`
      y en `GET /jobs`.
- [x] T13 — unidad `model-router-consumer.service` creada (`enable --now`, prefetch=1);
      gateway reiniciado (servía código viejo); 1 consumer por cola
      `jobs.{image,video,i2v,avatar}` confirmado en `rabbitmqctl list_queues`.
- [x] T14 — E2E real: POST /jobs/image draft 512x512 → 202 `{"id":"07c0efce...eb1"}` (32)
      → el consumer ejecutó el switch a qwen-image → `completed` con
      `.../model-router-output/07c0efce9e5347ba86af9e598c592eb1.png` (512x512 RGB, 178K,
      seed 428173246, 2m31s incluyendo carga de modelo).

### Verificación
- `pytest tests/test_rabbit_queue.py -q` → 9 passed (spec de la migración).
- `pytest tests/ -q` → **215 passed, 0 failed**.
- Los 6 fallos previos eran PRE-EXISTENTES, no causados por este pass: 5 esperaban las
  `BackgroundTasks` inline que la migración elimina (el POST ya no ejecuta el job) y 2 de
  esos eran el stale-reap desconectado. Se arreglaron: stale reap restaurado y los 4 tests
  de wiring/foto ahora empujan el job por el `make_handler` real del consumer.

### Route (ODD)
- Implementación INLINE (rabbit.py, consumer.py, router.py, tests): la delegación a un
  writer `general` falló dos veces con `405 Method Not Allowed` (transporte del task tool,
  no del prompt ni del repo) → se asumió inline.
- Ops (broker, systemd, restart, E2E): orquestador.


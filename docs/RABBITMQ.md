# RabbitMQ Migration Guide

## Current State (Redis-based queue)

The system hoy usa un **Redis-based job queue** en `src/jobs/queue.py` con las siguientes capabilities:

- **Enqueue**: `POST /jobs/image|video|i2v|avatar` → 202 + job_id
- **Status**: `GET /jobs/{id}` → status, attempts, last_error, result, error
- **Retry**: Lógica local en el consumer o llamada a `POST /jobs/{id}/retry` para reintentar un job fallado (máx 3 intentos).
- **Failure visibility**: `last_error` y `attempts` aparecen en `GET /jobs/{id}` y `GET /jobs/`.
- **Dead-letter**: Después de MAX_RETRIES (3), el job marca `status="failed"` y `last_error` describe el motivo.

This Redis queue is **shared** among all users and processes. It is **not** persistent across broker restarts in the sense of a message broker, pero los job state persiste en Redis (configurado con TTL de 7 days).

## Migration to RabbitMQ

When you add a new GPU machine or want multi-process concurrency, migrar a **RabbitMQ** es el siguiente paso. Esto es documentado aquí para que el cambio sea suave.

### Topología existente en el broker (heredada)

El broker RabbitMQ que levantaste ya tiene una topología lista para usarse, heredada de configuraciones anteriores. No es necesario declarar exchanges/queues fresh; vienen pre-configuradas:

| Element | Descripción |
|---------|-------------|
| `exchange` `jobs.delay` | Exchange directo con `x-message-ttl=60000` (60s) y `x-dead-letter-exchange="jobs"` |
| `queues` `jobs.delay.image/ video/ i2v/ avatar` | Cada una bindada al `jobs.delay` exchange con routing_key `"image"`/`"video"`/etc. TTL de 60s automático; tras expirar, el mensaje es dead-lettered al exchange `jobs` con routing_key `"jobs.<type>"`. |
| `exchange` `jobs` | Exchange directo compartido; bindings a `jobs.failed` para DLQ. |
| `queue` `jobs.failed` | Dead-letter queue compartida; todas las routes `"jobs.*"` bindean acá tras TTL + nack requeue=False. |

### How to publish (from your code)

Usá `src.jobs.rabbit.publish_job(job_type, payload, job_id)` con:

- `job_type` in `("image", "video", "i2v", "avatar")`
- `routing_key` = `job_type` → el mensaje va a `jobs.delay.<type>` (ej: `jobs.delay.image`)
- `exchange` = `"jobs.delay"` (el exchange heredado)
- `delivery_mode=2` (persistent)

Ejemplo desde router.py:

```python
from src.jobs import rabbit
ok = rabbit.publish_job("image", {"prompt": "hola", "quality": "draft"}, job_id="my-uuid")
```

El broker se encarga automaticamente del TTL y DLQ: si el consumer nack sin requeue, el mensaje expira tras 60s y es dead-lettered a `jobs.failed`.

### How to consume (from your code)

Usá `src.jobs.consumer --kinds image --prefetch 1` (o video/i2v/avatar). El consumer:

- Consumirá de la queue heredada `jobs.delay.image` (etc).
- Tras cada fallo, hará nack(requeue=False); el broker re-deliverá después de 60s (x-message-ttl).
- Llevá un contador local de reintentos (hasta MAX_RETRIES=3).
- Actualizará Redis con `status="retrying"`, `attempts=N`, `last_error=<motivo>` para visibilidad al front.
- A los 3 agotados, marcará job "failed" y dejará mensaje en `jobs.failed`.

### Scaling to N GPUs / Machines

Para correr N consumers (uno por GPU/machine):

1. Cada consumer abre su propia conexión RabbitMQ y declara su propio channel.
2. El broker load-balances messages across los consumers con prefetch=1 (asegura serialización por GPU).
3. Corré `python3 -m src.jobs.consumer --kinds image --prefetch 1` en cada máquina.
4. Los messages para `jobs.delay.image` serán distribuidos round-robin entre los consumers; cada uno procesará y respetará el lock de VRAM de su propia GPU.

### Monitoring & DLQ inspection

- **Management UI**: `http://127.0.0.1:15672` (guest/guest por defecto en dev).
- Ver mensajes en `jobs.failed` queue: en la UI o `rabbitmqctl list_queues name messages`.
- Ver mensajes pending en `jobs.delay.image`: `rabbitmqctl list_queues name messages`.
- Si un job falla repetidamente, verás `attempts=3` y `last_error` en `GET /jobs/{id}` (Redis). El mensaje quedará en `jobs.failed` para debugging.

### Migration Steps (no downtime)

1. **Levantar RabbitMQ** (ya lo tenés: `docker compose -f docker-compose.rabbitmq.yml up -d`).
2. **Probar publish/consume** con el `src.jobs.rabbit.publish_job` y `src.jobs.consumer` en modo debug, sin quitar el Redis queue actual.
3. **Añadir el endpoint `POST /jobs/{id}/retry`** (ya está implementado) para que los usuarios puedan reintentar jobs fallados desde el front.
4. **Gradual cutover**: Poné el `POST /jobs/*` para que use `rabbit.publish_job` en lugar de `queue.enqueue`. El `GET /jobs/{id}` ya muestra `attempts/last_error` tanto si el job fue creado por Redis como por RabbitMQ.
5. **When ready**: Levantar N consumers RabbitMQ y quitar el `JobQueue` Redis o mantenerlo solo para visibilidad histórica. El flujo de negocio (front → API → broker → consumer → result) quedaría intacto.

## Questions / Gotchas

- **¿Los jobs creados en Redis siguen visibles?** Sí, `GET /jobs/{id}` lee de Redis independientemente de quién los creó. El nuevo `attempts/last_error` field funciona para ambos.
- **¿Necesito borrar las queues viejas?** No, la topología heredada ya está lista. Si querés declarar fresh, hacé `docker exec model-router-rabbitmq rabbitmqctl reset` (pierde todos los messages, úsalo solo en mantenimiento).
- **¿Qué pasa con los jobs en curso?** Se pueden reenviar manualmente con `POST /jobs/{id}/retry` o dejarlos expirar en Redis (TTL 7 días). No hay riesgo de duplicados si usás el `job_id` único.
- **¿Credenciales seguras?** Las credenciales `guest/guest` son por defecto para dev. Sobreproduction, sobreescalá `RABBITMQ_USER` / `RABBITMQ_PASS` por vars de entorno y no commitear nunca los reales.

---
*Esta guía fue generada el 2026-10-12 para el proyecto model-router. La solución Redis actual (`src.jobs.queue.JobQueue`) sigue funcionando; RabbitMQ es opcional para escala multi-GPU.*
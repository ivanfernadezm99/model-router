# Design: Batch Media Burst

## Context

Gateway FastAPI :8000 multiplexa RTX 3090 24GB entre `coder-*` (:8082) y `wan-14b`/`sdxl` (:8188) con lock exclusivo. Queue actual (`src/gateway/queue.py`) ya implementa FIFO `asyncio.Queue`, `swapping_target`, `swap_started_at`, `coalesced`, `Retry-After 1..30`, `drain_fifo` y `is_expired`. `app.py` ya encola 202 si `lock.locked() or is_swapping()`. Lifecycle ya coalescea vía `active_model==target` check bajo lock. El comportamiento de burst 5+10 existe pero no está formalizado, observable ni documentado como contrato batch.

## Decision

**Mantener serialización con coalesce + 202, agregar observabilidad y helper cliente.** Sin paralelismo, sin persistencia, sin batch endpoint nuevo.

- Alternativa batch endpoint `/v1/batch` (server encola y proxy-secuencial): mayor throughput pero cambia semántica de 202 y requiere almacenar `Request` (no factible tras 202). Rechazada para este change; queda como follow-up si se mide necesidad.
- Alternativa queue persistente (Redis/disco): resuelve pérdida en crash pero añade dependencia operativa. Rechazada: gateway es stateless por diseño; pérdida de queue en crash es aceptable y callers reintentan 202 → nuevo encolado.
- Alternativa SSE/long-poll en vez de 202: evita retry cliente pero mantiene conexión ocupada durante 30-60s load → agota workers. Rechazada.

## Architecture

```
Client burst (15) → :8000 /v1/*
 → detector (header > prefix, case-insensitive, registry valid)
 → app.v1_proxy:
     if locked or swapping → queue.enqueue(target) → 202 + Retry-After + X-Queue-Depth + X-Target-Model
     elif active==target → proxy fast path (200, streaming SSE)
     elif active is None & target healthy → adopt & proxy
     else → set_swapping(target) → orchestrator.switch_to(target) (Lock, stop-before-start, VRAM guard, poll 2s/240s)
           → on ok: proxy 200, clear_swapping
           → on timeout: 504 o fallback code + X-Fallback
     metrics: queue_depth gauge, coalesced counter, swaps_total
```

- `queue.py`: ya tiene `_retry_after()` determinista `min(30, max(1, 240-elapsed))`. Se expone `depth()`, `coalesced`, `swaps_total` a metrics. Agregar `headers_for_202(target)` helper si se desea centralizar headers.
- `app.py`: enriquecer 202 con `X-Queue-Depth` y `X-Target-Model` (sin romper payload). Mantener `clear_swapping()` en `finally` (ya existe). No auto-proxy de encolados (callers ya recibieron 202, no hay Request almacenado).
- `metrics.py`: agregar `model_router_queue_depth`, `model_router_coalesced_total`, y opcional `model_router_swaps_total`. `health_payload` ya incluye queue_depth; agregar coalesced.
- `lifecycle.py`: sin cambios (lock + stop-before-start + VRAM guard + holds ya correctos).

## Data Flow — 5 videos + 10 imágenes

1. T0: Req V1 (video wan-14b) toma lock, `set_swapping(wan-14b)`, `stop code` → `start wan-14b` → poll 2s → ~30-60s → active=wan-14b.
2. T0+ms: V2-V5 llegan, `locked or swapping && target==wan-14b` → `enqueue` (coalesced+=4) → 202+Retry-After, depth 4. No start adicional.
3. T0+ms: I1-I10 (sdxl) llegan, `swapping_target==wan-14b != sdxl` → `enqueue` igual (no coalesce cross-target) → 202, depth 14.
4. T1: V1 proxy 200, `clear_swapping()`, drain FIFO vacía queue en <5s (arrival order) pero callers ya tienen 202.
5. T1+Retry-After: I_retry llega, `active==wan-14b != sdxl` → `set_swapping(sdxl)` → `stop wan-14b` → `start sdxl` → poll → active=sdxl → proxy 200.
6. T1+ms: I2-I10 retries coalesce en sdxl (mismo swap) → 202 hasta que sdxl activo, luego fast path 200.

Total: 2 starts, no 15. Invariante VRAM ≤1 siempre.

## Risks

- Thundering herd en retry sincronizado: mitigado con `Retry-After` escalonado por `elapsed` + jitter cliente 0..1s (docs/helper). No se randomiza server `Retry-After` para test determinista.
- Queue en memoria: pérdida en crash → callers reciben error y reintentan; aceptable y documentado.
- 240s hard timeout: caller debe respetar `is_expired` y fallback a code si aplica; metrics exponen timeouts.

## Testing

- `tests/test_batch_burst.py` (nuevo): burst 5 mismo ms (1×200 +4×202), burst 5+10 (2 starts), drain FIFO order + <5s, Retry-After 1..30 determinista, headers X-Queue-Depth/X-Target-Model, metrics gauges.
- Reuso `tests/test_e2e_queue.py` existente (<180s mocked, FIFO drain, 202 coalesce) — se mantiene verde.
- Mock systemctl/health/VRAM para determinismo; sin GPU real.

## Observability

- `/health`: `{model, vram_used_mb, queue_depth, coalesced, status}`.
- `/metrics`: `model_router_queue_depth`, `model_router_coalesced_total`, `model_router_swaps_total`, `model_router_vram_used_mb`.
- 202: `Retry-After`, `X-Queue-Depth`, `X-Target-Model`. Logs JSON con `request_id`, `hint`, `target_model`, `queue_depth`, `latency_ms`, `queued`.

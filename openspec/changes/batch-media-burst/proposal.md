# Proposal: Batch Media Burst — 5 videos + 10 imágenes con cola 202

## Why

El gateway serializa por VRAM exclusiva (RTX 3090 24 GB, 1 modelo a la vez, `asyncio.Lock` + `systemctl --user stop/start`). Un burst de 5 videos + 10 imágenes hoy funciona pero depende de que el **cliente maneje `202 + Retry-After` correctamente**. Sin contrato formal, documentado y testeado, un batch grande puede:

- provocar thundering herd (15 retries al mismo ms),
- perder observabilidad (coalesce no visible en metrics),
- confundir el cliente (no hay helper/retry recomendado),
- violar el timeout hard 240s sin payload claro.

Este proposal formaliza el comportamiento de batch burst como contrato de producto y agrega el mínimo necesario del lado server (headers/metrics/docs) + un helper cliente opcional, **sin paralelizar VRAM**.

## What Changes

- **Formalizar contrato batch burst**: 1er request toma lock y hace `stop-before-start` (~30-60s load, health poll 2s, hard timeout 240s). Requests concurrentes al mismo target durante swap → `202 Accepted` + `Retry-After: 1..30` + `{"queued":true,"target":"wan-14b|sdxl","retry_after":N}`. Coalesce: N requests al mismo target durante el mismo swap generan **1 solo `systemctl start`**. 5 videos + 10 imágenes → 2 switches (`wan-14b` → `sdxl`), no 15. Drain FIFO `< swap+5s` en orden de llegada (pero callers ya recibieron 202 y deben reintentar).
- **Server: headers y métricas** para batch observability: `Retry-After` consistente, `X-Queue-Depth`, `X-Target-Model` en 202, `model_router_coalesced_total` y `model_router_queue_depth` en `/metrics`.
- **Cliente: helper retry** documentado (no obligatorio): `scripts/batch_client.py` o snippet `curl` que hace `202 → sleep Retry-After → retry` con jitter y límite 240s.
- **Tests E2E** que prueban burst 5+10 coalesce → 2 switches, thundering herd al mismo ms (1 pasa, N reciben 202), y drain FIFO.
- **Docs**: README + spec de comportamiento nominal vs burst.

## Non-Goals

- Paralelizar VRAM (no hay 2 modelos a la vez).
- Persistir queue en disco/Redis.
- Batch endpoint `/v1/batch` server-side (se deja como follow-up si se necesita throughput mayor).
- Cambiar `coder-*` vs `wan-14b` vs `sdxl` registry.

## Scope

- `src/gateway/queue.py` — exponer depth/coalesced, Retry-After determinista, headers de observabilidad
- `src/gateway/app.py` — enriquecer 202 payload/headers, métricas de batch
- `src/gateway/metrics.py` — nuevos counters/gauges para coalesce y queue
- `src/orchestrator/lifecycle.py` — sin cambios funcionales (ya hace coalesce vía `active_model == target` check)
- `tests/test_e2e_queue.py` + `tests/test_batch_burst.py` (nuevo)
- `README.md` + `templates/batch_client.py` (helper opcional)
- `config.yaml` — sin cambios

## Impact

- **Shallow**: gateway `queue.py` + `app.py` + `metrics.py`. Sin cambios en lifecycle/VRAM/holds/health.
- **Riesgo bajo**: comportamiento ya existente; se formaliza y se hace observable. No se toca lock ni systemctl.
- **Auto-chain no requerido**: estimado < 250 líneas cambiadas. `single-pr` alcanza, pero `auto-chain` elegido por sesión permite slice si se divide helper vs server.

## Success Criteria

- Burst 5 videos exacto al mismo ms: 1×200 (proxy) + 4×202 con `Retry-After 1..30`, `queue_depth==4`, `coalesced==3` (o >=1), 1 solo `start wan-14b`.
- Burst 5+10 mixto: 2 switches totales, FIFO drain `< swap+5s`, cada caller que reintenta tras `Retry-After` eventualmente obtiene 200 (mock health instant).
- `/metrics` expone `model_router_queue_depth` y `model_router_coalesced_total`.
- Docs explican contrato 202 y helper cliente con jitter.

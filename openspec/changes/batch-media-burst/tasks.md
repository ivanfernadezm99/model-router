# Tasks: Batch Media Burst

## 1. Observability headers & metrics (server)
- [ ] 1.1 Enriquecer `src/gateway/app.py` 202: agregar `X-Queue-Depth: <depth>` y `X-Target-Model: <target>` headers junto a `Retry-After` (sin romper payload `{"queued":true,...}`).
- [ ] 1.2 Exponer `src/gateway/metrics.py`: `model_router_queue_depth` gauge (queue.depth()), `model_router_coalesced_total` counter (queue.coalesced), `model_router_swaps_total` counter (queue.swaps_total). Actualizar `health_payload` para incluir `coalesced`.
- [ ] 1.3 Verificar `src/gateway/queue.py` `_retry_after()` ya es `min(30, max(1, 240-elapsed))` y que `enqueue` incrementa `coalesced` solo si `target==swapping_target`.

## 2. Docs & helper cliente
- [ ] 2.1 Actualizar `README.md` sección "Cola durante swap → 202": documentar contrato batch burst (5+10 → 2 switches, coalesce, drain <5s, VRAM exclusiva), payload/headers 202, y patrón retry `sleep(Retry-After + jitter 0..1s)` hasta 240s.
- [ ] 2.2 Crear `templates/batch_client.py` (opcional, referencia): `async def batch_requests(urls, headers, payloads)` que hace `POST`, si `202` lee `Retry-After`, `sleep(Retry-After+random.uniform(0,1))` y reintenta hasta 200 o 240s, con `X-Request-Id` propio.

## 3. Tests E2E burst
- [ ] 3.1 Crear `tests/test_batch_burst.py`:
  - `test_burst_5_videos_same_ms_one_start`: 5 concurrent `X-Model-Hint: video` al mismo ms → 1×200 +4×202, `Retry-After 1..30`, `X-Queue-Depth` presente, `start wan-14b ==1`, `coalesced>=1`.
  - `test_burst_5_videos_10_images_two_switches`: burst mixto 5 wan-14b +10 sdxl → `swaps_total==2` (no 15), FIFO order preservado, drain `<swap+5s`.
  - `test_retry_after_determinista_y_headers`: `set_swapping` + `elapsed` → `Retry-After == min(30, max(1,240-elapsed))`, payload `retry_after` == header.
  - `test_metrics_y_health_exponen_queue`: `/metrics` contiene `model_router_queue_depth` y `coalesced_total`; `/health` incluye `queue_depth` y `coalesced`.
- [ ] 3.2 Asegurar `tests/test_e2e_queue.py` existentes siguen verdes (`pytest -q`).

## 4. Validación & entrega
- [ ] 4.1 `python3 -m pytest -q` y `python3 -m pytest tests/test_batch_burst.py tests/test_e2e_queue.py -v` verdes (<180s por switch mocked).
- [ ] 4.2 `curl` manual: `for i in 1..5; do curl -i -H "X-Model-Hint: video" ... & done` → verificar 1×200 +4×202.
- [ ] 4.3 Verificar `openspec/changes/model-router` no roto; `batch-media-burst` no toca lifecycle/VRAM/holds.

## Review Workload Forecast
- Changed lines est.: ~180 (queue/metrics/app/docs) + ~120 tests = ~300. Under 400 → single PR.
- Risk: Low (shallow, ya coalescea, sin lock changes).
- Decision needed before apply: No (auto-chain permitido pero single-pr suficiente).

## Dependencies
- proposal → spec → design → tasks → apply (este archivo) → verify → archive

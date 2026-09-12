# Spec: Batch Queue — Burst 5 videos + 10 imágenes

## Requirement: Burst coalesce same-target
- WHEN  N requests concurrentes al mismo target llegan mientras `orchestrator.lock` o `queue.is_swapping()==True` y `target == swapping_target` THEN  el sistema MUST retornar `202 Accepted` a N-1 callers y ejecutar **exactamente 1** `systemctl start` para ese target. MUST incrementar `coalesced` en `queue.coalesced` por cada request coalesceada. MUST NOT ejecutar start adicional por cada coalesceada.

## Requirement: 202 contract
- WHEN  request llega durante swap THEN  MUST responder `202` con headers `Retry-After: 1..30` (enteros) y JSON `{"queued":true,"target":"<target>","retry_after":<Retry-After>}`. `retry_after` MUST ser `min(30, max(1, 240 - elapsed_swap))`. El payload MUST ser consistente con el header.

## Requirement: VRAM exclusive
- WHEN  cualquier burst mixto (ej 5 videos wan-14b + 10 imágenes sdxl) THEN  el sistema MUST mantener invariante `≤1 modelo en VRAM` en todo momento (lock exclusivo, `stop-before-start`).

## Requirement: Drain FIFO
- WHEN  swap termina (poll_health ok o timeout) THEN  `queue.drain_fifo()` MUST vaciar la cola en orden FIFO de `request_id`/arrival en `< 5s` adicional tras swap. `queue.depth()` MUST reflejar FIFO length en todo momento.

## Requirement: Hard timeout 240s
- WHEN  swap excede `HARD_TIMEOUT_S=240` THEN  MUST retornar `504 model load timeout` al holder y, si `task in (image,video)` y fallback `code` está sano, MUST intentar proxy a fallback con header `X-Fallback: local-llm` (fallback ya existente). Queue MUST limpiar `swapping_target`.

## Requirement: Observability
- WHEN  burst ocurre THEN  `/metrics` MUST exponer `model_router_queue_depth` (gauge) y `model_router_coalesced_total` (counter). `GET /health` MUST incluir `queue_depth` y `coalesced` en payload. Cada `202` SHOULD incluir `X-Queue-Depth` y `X-Target-Model` headers.

## Requirement: Client retry helper
- WHEN  cliente recibe `202` THEN  docs MUST especificar: `sleep(Retry-After + jitter 0..1s) → retry` hasta `240s` o hasta `200`. Helper opcional `templates/batch_client.py` MUST implementar ese loop.

## Scenario: 5 videos al mismo ms
```
GIVEN active_model=coder-14b
WHEN 5 POST /v1/* con X-Model-Hint: video llegan al mismo ms
THEN 1 request hace lock+stop+start wan-14b y proxy 200
 AND 4 reciben 202 con Retry-After 1..30, queue_depth 4, coalesced >=1
 AND systemctl start wan-14b == 1
```

## Scenario: 5 videos + 10 imágenes
```
GIVEN burst mixto
WHEN 1er video dispara swap a wan-14b
THEN 4 videos coalesce (202), 10 imágenes encoladas (202) con target sdxl
WHEN video swap termina y caller retry de image llega
THEN dispara stop wan-14b → start sdxl (2do switch)
 AND 9 imágenes restantes coalesceadas (202) hasta retry
 AND total starts == 2
 AND drain FIFO < swap+5s
```

## Scenario: Retry eventual success
```
GIVEN caller recibió 202 con Retry-After 12
WHEN espera 12s + jitter y reintenta (target ya activo)
THEN recibe 200 proxy (fast path active_model==target)
```

## Scenario: Expiración
```
GIVEN item en queue con created_at hace >=240s
WHEN se evalúa is_expired
THEN MUST retornar true (caller debería recibir 504 si reintenta tras expiración)
```

## Out of scope
- Paralelismo VRAM
- Persistencia queue
- Batch endpoint agregado

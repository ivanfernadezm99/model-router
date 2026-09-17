# Valoración Combo y OPENCODE — Lógica Persistente

**Fecha:** 2026-09-16  
**Estado:** Activo, testeado con `tests/test_opencode_valuation.py` (9 tests)

## Objetivo
Que el combo `0. Modelo cargado en VRAM` siempre muestre:
- Valoración 1-5 con estrellas (★★★★★)
- Cuál es apto para opencode al lado

## Implementación
- **`web/templates/index.html`** — `qualityScore()` y `qualityTag()`:
  - `coder-30b-a3b`: 5.0/5 ⭐ fino — mejor calidad code
  - `coder-30b-q5`: 5.0/5 💎 Q5 máx calidad
  - `coder-14b-100k`: 3.5/5 ✅ 14B estable 🤖 OPENCODE — estable para agents
  - Genera `o.textContent = "● coder-14b-100k — 100K ctx — 20.0GB — 3.5/5 ✅ 14B estable 🤖 OPENCODE — estable para agents ★★★★☆"`
  - `activeDesc` muestra `Calidad: 3.5/5 ✅ 14B estable 🤖 OPENCODE ★★★★☆`

- **`web/app.py` MODEL_DESCS**:
  - `coder-14b-100k`: "🤖 OPENCODE — Qwen2.5-Coder-14B (Q4) 100K todo en GPU... Recomendado para agents (explore/general)."

- **`config.yaml` defaults.model**: `coder-14b-100k` (opencode default, 30b queda manual via front)

- **`src/gateway/detector.py` TASK_TO_MODEL**: `code -> coder-14b-100k` + passthrough en `app.py` para no hacer thrash entre coder* variants

- **`src/gateway/proxy.py`**: fix streaming — `client = httpx.AsyncClient` sin `async with`, cierre en `finally` del `aiter` y en cada `except` (evita 0 bytes/stream_read_error)

## Bench para elegir
- **Script:** `bench_30b_vs_14b.sh` (ejecutable, espera 300s con `wait_health`)
- **Payloads:** `bench_opencode.py` (plain 21 tok, tools 153 tok, large 2020 tok)
- **Resultados 2026-09-16:**
  - 14b: 3.5/5, 60 tok/s, stream 113ms, tool_calls:true, prompt 0.54ms/tok
  - 30b: 5.0/5, 43 tok/s, stream 316ms, prompt 1.57ms/tok, 69.6% SWE-bench pero 3× más lento en prompt gigante
  - Recomendación: opencode → 14b, code crítico → 30b manual

## Tests
`pytest tests/test_opencode_valuation.py` — 9 tests que fallan si se borra la valoración o el tag OPENCODE.
`pytest tests/test_gateway.py::test_task_to_model` — asegura code sigue mapeando a 14b.

## Timeout para LLMs lentos (2026-09-16)
- **Opencode** `~/.config/opencode/opencode.json`: `provider.llm-local.options.timeout/headerTimeout/chunkTimeout = 600000` (10min, default 300000)
- **Gateway** `src/gateway/proxy.py` `TIMEOUT_S=600`, `src/orchestrator/health.py` `HARD_TIMEOUT_S=600 NOMINAL=300`
- Reiniciar gateway: `systemctl --user restart model-router-gateway` y opencode

## Operación
- Reiniciar web tras editar `index.html`: `kill $(lsof -ti:5000); nohup python3 web/app.py &`
- Switch fiable: `curl -X POST http://127.0.0.1:8000/jobs/switch -d '{"model":"coder-30b-a3b"}'` tarda 60-138s, el bench lo espera.
- Health: `curl http://127.0.0.1:8000/health` debe mostrar `coder-14b-100k ready` por default.

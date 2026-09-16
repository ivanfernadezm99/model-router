# Model Router — GPU Gateway :8000

Stateless FastAPI gateway que multiplexa una RTX 3090 24 GB entre `code` (llama-server :8082) e `image/video` (ComfyUI/SDXL :8188). Un solo modelo ocupa VRAM; el resto se orquesta vía `systemctl --user stop/start` con lock exclusivo, queue FIFO 202 y health poll 2s hasta 180s nominal / 240s hard timeout.

## Arquitectura

```
Client → :8000 /v1/* → Detector (X-Model-Hint | prefix code:/image:/video: | default code)
                    → Registry (config.yaml → service/port/vram_mb/args -np 1)
                    → Orchestrator (asyncio.Lock + VRAM guard nvidia-smi + IdleReaper 10m)
                    → systemctl --user stop/start → poll /health → proxy httpx streaming
                    → /health /metrics
```

- `src/gateway/app.py` — FastAPI lifespan + IdleReaper
- `src/gateway/detector.py` — header gana a prefix, case-insensitive, valida registry
- `src/gateway/queue.py` — FIFO `asyncio.Queue`, coalesce mismo target, `Retry-After 1..30`, drain <swap+5s
- `src/gateway/proxy.py` — `httpx.AsyncClient` streaming `text/event-stream`, bearer passthrough, 500/502/504 verbatim
- `src/orchestrator/lifecycle.py` — `asyncio.Lock` stop-before-start, `holds_ok()` y `validate_np`
- `src/orchestrator/health.py` / `vram.py` / `idle.py`
- `config.yaml` — `coder-q4-131k` 14 GB, `coder-q5-65k` 18 GB, `sdxl` 6.5 GB

## Uso

```bash
# instalar
pip install fastapi uvicorn httpx pyyaml

# validar config
python -c "from src.registry.registry import Registry; Registry('config.yaml').load(); print('ok')"

# correr gateway (dev)
uvicorn src.gateway.app:app --host 127.0.0.1 --port 8000 --workers 1

# systemd user
systemctl --user daemon-reload
systemctl --user enable --now model-router-gateway.service
systemctl --user status model-router-gateway.service
curl -s http://127.0.0.1:8000/health | jq
curl -s http://127.0.0.1:8000/metrics
```

## Curl ejemplos

```bash
# code — default (sin hint ni prefix)
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer tok" \
  -d '{"prompt":"hello","stream":false}'

# code vía header (case-insensitive, gana a prefix)
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Model-Hint: CoDe" -H "Content-Type: application/json" \
  -d '{"prompt":"image: hello"}'

# image vía prefix cuando no hay header
curl -s http://127.0.0.1:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"prompt":"image: a cat in space"}'

# video vía header
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Model-Hint: video" -H "Content-Type: application/json" \
  -d '{"prompt":"animate a cat"}'

# streaming SSE (sin buffering)
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"prompt":"code: fib","stream":true}'

# cola durante swap → 202 + Retry-After (contrato batch)
curl -i http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Model-Hint: image" -H "Content-Type: application/json" \
  -d '{"prompt":"image: dog"}'
# HTTP/1.1 202 Accepted
# Retry-After: 12
# X-Queue-Depth: 3
# X-Target-Model: sdxl
# {"queued":true,"target":"sdxl","retry_after":12}
# Cliente DEBE hacer: sleep(Retry-After + jitter 0..1s) → retry hasta 200 o 240s
# Ver templates/batch_client.py para helper async con jitter

# health / metrics
curl -s http://127.0.0.1:8000/health
# {"model":"sdxl","vram_used_mb":6800,"queue_depth":0,"status":"ready"}
curl -s http://127.0.0.1:8000/metrics | grep model_router
```

## Batch burst 5 videos + 10 imágenes — contrato

El gateway **serializa** (1 modelo en VRAM). No hay paralelismo.

```
5 videos (wan-14b) + 10 imágenes (sdxl) al mismo ms:
  1er video → lock → stop code → start wan-14b (~30-60s) → 200 proxy
  otros 4 videos → 202 + Retry-After (mismo target wan-14b) → coalesce: 1 solo start, 4 esperan retry
  10 imágenes → 202 encoladas (target sdxl)
  cuando termina video, retry de image → stop wan → start sdxl → 200
  9 imágenes restantes coalesceadas → 202 hasta retry → 200
  Resultado: 2 switches (video→image), no 15. Drain FIFO <swap+5s.
```

Cliente **debe** manejar `202 → sleep(Retry-After+0..1s jitter) → retry` (ver `templates/batch_client.py`).
Si mandás 5 videos exacto al mismo ms, 1 pasa y 4 reciben 202 — sin retry el batch parece "no andar".
Límites: VRAM exclusiva, queue en memoria (no persiste), timeout hard 240s.

## v1 vs /jobs — cuándo usar cada uno

- **`POST /v1/*` (gateway síncrono)** → **usar para LLM (code) y para media síncrona**. Este es el que tiene `X-Model-Hint`, coalesce, `202 + Retry-After` y proxy streaming. **LLM SIEMPRE va por `/v1`** (`X-Model-Hint: code` o sin header, default code). Jobs NO es para LLM.
- **`POST /jobs/image` y `POST /jobs/video` (async Redis)** → cola persistente para batches largos, devuelve `202 {id}` y se consulta con `GET /jobs/{id}`. **Ahora cableado al Orchestrator** (mismo `asyncio.Lock` y `switch_to` que `/v1`), así que `5 videos +10 imágenes` vía `/jobs` también serializa a 2 switches (`wan-14b → sdxl`) con estados `queued → running → completed/failed` en Redis (TTL 7d). Requiere `redis-server`/`valkey` en `:6379` (ya corre en este host). **Usar `/jobs` para batch async** (fire-and-forget + polling), **`/v1` para sync/LLM/streaming**.

```bash
# /jobs async batch (ahora sí funciona)
curl -s http://127.0.0.1:8000/jobs/video -H "Content-Type: application/json" -d '{"prompt":"animate cat"}'
# {"id":"a1b2c3..."}
curl -s http://127.0.0.1:8000/jobs/a1b2c3... | jq
# {"id":"...","status":"running","created_at":"..."} → luego "completed" con {"target":"wan-14b","payload":{...}}
# para imágenes con calidad
curl -s http://127.0.0.1:8000/jobs/image -H "Content-Type: application/json" -d '{"prompt":"cat in space","quality":"balanced"}'
curl -s http://127.0.0.1:8000/jobs/image -H "Content-Type: application/json" -d '{"prompt":"cat","quality":"draft","width":512,"height":512,"steps":20}'

# LLM arreglado — siempre por /v1 (no por /jobs)
curl -s http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" -d '{"prompt":"hola","stream":false}' | jq
# default code → coder-14b-100k en :8082 (si no hay X-Model-Hint ni prefix)
```

**Diferencia clave**: `/v1` bloquea con `202 Retry-After` (cliente reintenta); `/jobs` devuelve `id` inmediato y el worker en `BackgroundTasks` hace `switch_to` con el mismo lock, así el cliente hace polling sin ocupar conexión.

## VRAM (RTX 3090 24 GB)

| Modelo | VRAM | Puerto | Args |
|--------|------|--------|------|
| coder-q4-131k | 14000 MB | 8082 | `-np 1 -c 131072` |
| coder-q5-65k | 18000 MB | 8082 | `-np 1 -c 65536` |
| sdxl | 6500 MB | 8188 | `--listen 127.0.0.1 --port 8188` |
| wan-14b | 20000 MB (pico 23900) | 8189 | `Wan2.1-T2V-14B, t5_cpu=True, offload, PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` |

Guard fail-closed: `nvidia-smi` garbage/`N/A`/returncode!=0 → `None` → bloquea `can_start`. Idle <2 GB verificado tras cada `stop` y por `IdleReaper`.

> **Wan 14B en 3090 — offload agresivo obligatorio:** el modelo ocupa 65 GB en disco y 23 GB en VRAM al cargar. Con `t5_cpu=True` + `model.cpu()/vae.cpu()` tras init + `offload_model=True` por step y `expandable_segments:True`, queda en 18 MiB idle y solo sube a ~20 GB durante los 20 steps. Default `num_frames=17` (4n+1, ~1s video) para entrar en 24 GB; `33` o `81` frames OOMean sin este offload. Ver `~/Wan2.1/wan_server.py` y `systemd/user/wan-video.service.d/override.conf`.

## Logs — qué pasa adentro

Gateway loguea JSON por cada request (ahora sí en `journalctl`):
```bash
journalctl --user -u model-router-gateway.service -f
journalctl --user -u model-router-gateway.service -o cat | grep request_id
# {"request_id":"a1b2c3d4","hint":"video","target_model":"wan-14b","queue_depth":2,"latency_ms":1234,"status":200,"swapped":true}
journalctl --user -u wan-video.service -f
nvidia-smi  # VRAM real
curl -s http://127.0.0.1:8000/health | jq
curl -s http://127.0.0.1:8000/metrics | grep model_router
```
`src/gateway/app.py` tiene `logging.basicConfig INFO` + `logger.info` en fast-path/adopted/swapped/queued/400/504. `PYTHONUNBUFFERED=1` en systemd asegura flush inmediato.

## Notificaciones Telegram — jobs async

`POST /jobs/video` y `/jobs/image` ahora mandan Telegram al terminar si configuras el bot (fire-and-forget, timeout 5s):
```bash
# ya configurado en este host (icemorphBot)
# TOKEN=7757499045:AAH...  CHAT_ID=1422594274  en /home/servidor/.env y systemctl --user set-environment
curl -s http://127.0.0.1:8000/jobs/video -H "Content-Type: application/json" -d '{"prompt":"a cat dancing"}'
# → {"id":"..."}  y luego por Telegram: "✅ video job ab12cd34 terminado" + Archivo: /tmp/wan_*.mp4
# o "❌ video job ... falló"

# para activar en otro host:
export XDG_RUNTIME_DIR=/run/user/$(id -u) && export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user set-environment TELEGRAM_BOT_TOKEN=TU_TOKEN
systemctl --user set-environment TELEGRAM_CHAT_ID=TU_CHAT_ID
systemctl --user daemon-reload && systemctl --user restart model-router-gateway.service
# polling siempre disponible:
curl -s http://127.0.0.1:8000/jobs/{id} | jq  # queued → running → completed/failed
```
Código: `src/jobs/worker.py:_notify_telegram()` y `httpx POST https://api.telegram.org/bot{token}/sendMessage`. Si no hay env, no notifica (silencioso).

## Holds — cuda-driver-580 + kornia==0.6.12

```bash
sudo apt-mark hold cuda-driver-580
apt-mark showhold | grep cuda-driver-580
pip freeze | grep kornia==0.6.12
# si falta → swap bloqueado y warning en logs; ComfyUI con kornia>=0.7 crashea en FX-8350 sin AVX2
sudo apt-mark hold cuda-driver-580  # evita driver auto-upgrade que rompe CUDA 12.6
pip install "kornia==0.6.12"         # pin por FX-8350
```

## Switch manual vs gateway

```bash
# manual (sin gateway) — exclusivo a mano
systemctl --user stop llama-code-q4.service
systemctl --user start sdxl.service
curl -s http://127.0.0.1:8188/system_stats | head

# gateway (recomendado) — un solo entrypoint :8000
# El gateway hace stop-before-start + poll 2s + proxy + 202 queue + fallback X-Fallback
curl -s http://127.0.0.1:8000/v1/chat/completions -H "X-Model-Hint: image" -d '{"prompt":"image: cat"}'
# image timeout 240s → fallback a code :8082 con header X-Fallback: local-llm (si code sano), 502 si code caído
```

Secuencia `code→image→video→code` con mocks: `pytest tests/test_e2e_queue.py -v` (<180s por switch, FIFO coalesce 5→1, drain <swap+5s).

## Tests

```bash
python3 -m pytest -q  # 48 passed
python3 -m pytest tests/test_e2e_queue.py -v
systemd-analyze verify systemd/user/*.service
grep -q "Restart=always" systemd/user/model-router-gateway.service && echo ok
```

## Operación segura — no romper nada

- **Nunca corras dos modelos a la vez manualmente:** usa siempre `:8000` (gateway hace `stop-before-start` con `asyncio.Lock`). `systemctl --user stop llama-code-q4 && start wan-video` manual rompe el invariante y deja 24 GB ocupados → OOM.
- **No toques `config.yaml` sin validar:** `python -c "from src.registry.registry import Registry; Registry().load(); print('ok')"`
- **Holds fijos:** `cuda-driver-580` y `kornia==0.6.12` — un upgrade rompe CUDA 13 o ComfyUI en FX-8350.
- **Logs antes de reiniciar:** `journalctl --user -u model-router-gateway.service -n 50` y `nvidia-smi`
- **Wan 14B:** no cambies `t5_cpu` ni `expandable_segments` sin probar con `17 frames` primero. `81 frames` OOMea en 3090 sin offload.

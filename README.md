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

# cola durante swap → 202 + Retry-After
curl -i http://127.0.0.1:8000/v1/chat/completions \
  -H "X-Model-Hint: image" -H "Content-Type: application/json" \
  -d '{"prompt":"image: dog"}'
# HTTP/1.1 202 Accepted
# Retry-After: 12
# {"queued":true,"target":"sdxl","retry_after":12}

# health / metrics
curl -s http://127.0.0.1:8000/health
# {"model":"sdxl","vram_used_mb":6800,"queue_depth":0,"status":"ready"}
curl -s http://127.0.0.1:8000/metrics | grep model_router
```

## VRAM (RTX 3090 24 GB)

| Modelo | VRAM | Puerto | Args |
|--------|------|--------|------|
| coder-q4-131k | 14000 MB | 8082 | `-np 1 -c 131072` |
| coder-q5-65k | 18000 MB | 8082 | `-np 1 -c 65536` |
| sdxl | 6500 MB | 8188 | `--listen 127.0.0.1 --port 8188` |

Guard fail-closed: `nvidia-smi` garbage/`N/A`/returncode!=0 → `None` → bloquea `can_start`. Idle <2 GB verificado tras cada `stop` y por `IdleReaper`.

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
python3 -m pytest -q
python3 -m pytest tests/test_e2e_queue.py -v
systemd-analyze verify systemd/user/*.service
grep -q "Restart=always" systemd/user/model-router-gateway.service && echo ok
```

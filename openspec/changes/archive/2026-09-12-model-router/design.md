# Design: GPU Model Router

## Technical Approach

Stateless FastAPI gateway on `:8000` is the sole OpenAI-compatible entry point. It classifies each request (`code|image|video`), consults an in-memory `Registry` loaded from `config.yaml`, and delegates to an `Orchestrator` that owns VRAM exclusively via `systemctl --user stop/start` + `nvidia-smi` verification. One `asyncio.Lock` serializes swaps; a FIFO `asyncio.Queue` holds concurrent arrivals (202 + Retry-After) until health polling (`/health` 180s nominal, 240s hard timeout) succeeds. Idle reaper frees VRAM after 10 min. No training, no multi-GPU, no workflow engine — only lifecycle.

Maps to proposal `Approach` (gateway → detect → check active → mismatch enqueue/stop/start/poll/proxy) and covers specs: `gateway-proxy` (proxy/queue/metrics), `model-lifecycle` (registry/systemd/health/idle), `task-routing` (hint/prefix/lock/fallback).

## Architecture

### Component Diagram

```
                   ┌─────────────────────────────────────────────┐
                   │            Clients (Open WebUI / Claw / CLI) │
                   └──────────────────────┬──────────────────────┘
                                          │ HTTP :8000  (127.0.0.1)
                                          ▼
                   ┌─────────────────────────────────────────────┐
                   │  FastAPI Gateway  :8000 (src/gateway/)       │
                   │  ┌──────────┐ ┌──────────┐ ┌────────────┐  │
                   │  │ Router   │ │ Queue    │ │ Metrics    │  │
                   │  │ /v1/*    │ │ FIFO 202 │ │ /health    │  │
                   │  │ proxy    │ │ Retry-Aft│ │ /metrics   │  │
                   │  └────┬─────┘ └────┬─────┘ └─────┬──────┘  │
                   │       │            │             │         │
                   │  ┌────▼────────────▼─────────────▼──────┐  │
                   │  │ Task Detector (X-Model-Hint / prefix) │  │
                   │  └────┬─────────────────────────────────┘  │
                   └───────┼────────────────────────────────────┘
                           │ resolves via
                           ▼
                   ┌──────────────────┐         ┌──────────────────┐
                   │ Registry         │◄────────│ config.yaml      │
                   │ config.yaml →    │  load   │ service, port,   │
                   │ model→service    │         │ health, vram_mb  │
                   │ port, vram, args │         │ args (-np 1)     │
                   └────────┬─────────┘         └──────────────────┘
                            │ lookup
                            ▼
                   ┌─────────────────────────────────────────────┐
                   │ Orchestrator  (src/orchestrator/)            │
                   │ ┌──────────┐ ┌──────────┐ ┌──────────────┐  │
                   │ │ asyncio  │ │ systemctl│ │ Health Poll  │  │
                   │ │ Lock     │ │ --user   │ │ 2s interval  │  │
                   │ │ exclusive│ │ stop/start│ │ 180s/240s   │  │
                   │ └────┬─────┘ └────┬─────┘ └──────┬───────┘  │
                   │      │            │               │         │
                   │ ┌────▼────────────▼───────────────▼──────┐  │
                   │ │ VRAM Guard (nvidia-smi) + Idle Reaper │  │
                   │ │ 10 min → stop → <2GB check            │  │
                   │ └───────────────────────────────────────┘  │
                   └──────────┬──────────────────┬───────────────┘
                              │ stop/start       │ health GET
              ┌───────────────▼──────┐   ┌───────▼───────────────┐
              │ Model Backends       │   │ systemd user units     │
              │ llama-server  :8082  │   │ llama-code-q4.service  │
              │ ComfyUI       :8188  │   │ llama-code-q5.service  │
              │ (Wan/LTX future)     │   │ sdxl.service           │
              └──────────────────────┘   │ Restart=always         │
                                         └───────────────────────┘
                              ┌──────────────────┐
                              │ Logs /tmp/*.log  │
                              │ JSON structured  │
                              └──────────────────┘
```

### Sequence: Swap `code` → `sdxl` with two queued callers

```
Client A (image)   Gateway :8000        Orchestrator (Lock)      systemd          sdxl :8188    nvidia-smi
    │ POST /v1/images/generations          │                       │                 │              │
    │ X-Model-Hint:image ─────────►│ detector → target=sdxl        │                 │              │
    │                    │ active=code → mismatch ──►│ acquire Lock          │                 │              │
    │                    │ enqueue (not yet)         │ systemctl stop code   │                 │              │
    │                    │                           │──────────────────────►│                 │              │
    │                    │                           │ wait exit + nvidia-smi│                 │──query VRAM──►│
    │                    │                           │ systemctl start sdxl  │                 │              │
    │                    │                           │──────────────────────►│   starting      │              │
    │                    │ poll /health 2s loop ──────────────────────────────────────────────►│              │
Client B (image)         │                    │       │◄─── 503 … 503 … 200 ───────────────────│              │
    │ POST … image ─────►│ swap in progress  │       │ ready <180s         │                 │              │
    │◄── 202 Retry-After:45 + queued FIFO ─│       │ release Lock? no→   │                 │              │
    │                    │                    │       │ drain FIFO         │                 │              │
    │                    │ proxy B → :8188 ────────────────────────────────────────────────►│              │
Client C (code)          │                    │                    │                 │              │
    │ POST … code ──────►│ swap holds Lock   │                    │                 │              │
    │◄── 202 Retry-After:30 ────────────────│                    │                 │              │
    │                    │ after sdxl ready, │ new swap code? acquire Lock again                │              │
    │                    │ detect → queue FIFO behind same mechanism                            │              │
```

Idle Reaper (background `asyncio.create_task`):
```
every 60s: if now - last_request_ts > 600s and active_model != None:
             orchestrator.stop(active_service) → nvidia-smi <2048 MB → /health status=idle
             any new request resets last_request_ts = now
```

## Architecture Decisions

### Decision: FastAPI + httpx streaming

**Choice**: FastAPI on `:8000` with `httpx.AsyncClient` streaming (`response.aiter_bytes()` / `StreamingResponse`).

**Alternatives considered**: `aiohttp` proxy, NGINX/OpenResty Lua, Go reverse proxy.

**Rationale**: Project is Python-native (ComfyUI, llama-server wrappers); `httpx` preserves `text/event-stream` without buffering and supports `stream=True` + header passthrough trivially. FastAPI gives typed dependency injection for `X-Model-Hint` parsing and auto-doc for `/health`/`/metrics`. NGINX would require Lua for header/prefix detection and FIFO queue logic, adding an alien runtime. Go would be fastest but doubles stack for a single-node router with no throughput bottleneck (bottleneck is 90–180s model load, not proxy throughput).

### Decision: systemctl --user vs Docker/Podman

**Choice**: `systemctl --user stop/start` on native systemd user units with `Restart=always`.

**Alternatives considered**: Docker containers with `docker stop/start`, Podman pods, raw `subprocess.Popen` + `kill`.

**Rationale**: Models already run as systemd user services on this host (proposal Dependencies). `systemctl --user` gives process supervision, `Restart=always` for crash recovery, journal integration, and user-level operation without root. Docker adds ~500 MB image overhead per model, breaks `nvidia-smi` host visibility inside container without `--gpus all` plumbing, and complicates `-np 1` validation (needs entrypoint patching). Raw `Popen` loses supervision and boot persistence. Tradeoff: ties to systemd Linux, but host is fixed single-node RTX 3090.

### Decision: Enforce -np 1

**Choice**: Registry validator rejects any `args` containing `-np` with value `>1`; orchestrator injects `-np 1` if absent.

**Alternatives considered**: Allow `-np 2` for parallel slots, rely on llama-server default.

**Rationale**: RTX 3090 24 GB cannot fit `coder-q4 (~14 GB with 131K ctx) + sdxl (~6.5 GB)` concurrently. `-np 1` enforces single sequence slot, keeping KV cache bounded. With `-np 2`, two concurrent prompts double KV cache and OOMs during 131K context even with q4. Proposal Risk table explicitly calls `-np>1` a medium-likelihood OOM; exclusive lock + `-np 1` is the only safe invariant. Cost: halves parallel throughput, but FIFO queue already serializes during swap — throughput is VRAM-bound, not slot-bound.

### Decision: q4 for 131K context, q5 for 65K

**Choice**: `coder-q4-131k` (q4_K_M) for 131072 context; `coder-q5-65k` (q5_K_M) for 65536.

**Alternatives considered**: q5 for both, q8 for 131K, single q4 for all contexts.

**Rationale**: VRAM budget for LLM = `weights + KV cache`. KV cache ≈ `2 * n_layers * n_heads * head_dim * ctx * bytes_per_elem`. At 131K, KV cache alone is ~8–10 GB in fp16; q4 reduces weight footprint from ~22 GB to ~14 GB, leaving headroom for KV. q5 at 131K would be ~18 GB weights + 10 GB KV = 28 GB > 24 GB → OOM. At 65K, KV is ~4–5 GB, so q5 fits (~18 + 5 = 23 GB) and gives higher quality. Two variants let callers choose: long-context cheap, short-context accurate.

### Decision: Holds — apt-mark hold + kornia pin

**Choice**: Boot check verifies `apt-mark showhold | grep cuda-driver-580` and `pip freeze | grep kornia==0.6.12`; warning + block swap if missing. Units document holds in `README`.

**Alternatives considered**: Container freeze, Nix pinning, ignore holds.

**Rationale**: Failure modes observed: driver 580 auto-upgrade breaks CUDA 12.6 compatibility → `nvidia-smi` fails, all models down; `kornia>=0.7` requires AVX2 (FX-8350 lacks it) → ComfyUI import crash. `apt-mark hold` and `kornia==0.6.12` pin are lowest-friction guards on a bare-metal host. Nix would be hermetic but 10× migration cost (proposal Alternative `K8s/Triton` rejected for same reason).

### Decision: Asyncio primitives over external queue

**Choice**: `asyncio.Lock` for exclusive swap + `asyncio.Queue` (FIFO) + `asyncio.Event` for health ready; `asyncio.create_task` for idle reaper.

**Alternatives considered**: Redis queue, Celery, file lock (`flock`).

**Rationale**: Single-process gateway; no need for distributed queue. `asyncio` primitives are zero-dependency, preserve ordering without serialization, and coalesce same-target concurrent starts (check `lock.locked()` → enqueue instead of second `systemctl start`). Redis would add a service that itself needs lifecycle management.

## Data Flow

1. Request arrives `:8000` → `TaskDetector` reads `X-Model-Hint` (case-insensitive) else scans `prompt`/`messages[0].content` prefix `code:|image:|video:` else defaults `code` (logs `routing:default:code`).
2. `Registry.resolve(task)` → `{service, port, health_endpoint, vram_mb, args}`. Unknown → `400 unknown model`, no systemctl.
3. If `target == active` and `health==ready` → `httpx` proxies immediately (stream passthrough, bearer forwarded, `X-Fallback` absent).
4. Else if `lock.locked()` → enqueue FIFO, return `202 {queued:true, retry_after: <remaining_swap_s>}  `Retry-After` header. Same-target requests coalesce (no second `start`).
5. Else acquire `lock`, `systemctl --user stop <active>`, poll `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` until `<2048` or stop confirmed, `systemctl --user start <target>`, poll `http://127.0.0.1:{port}{health_endpoint}` every 2s until 200 or 240s. On 200 → update `active`, `last_request_ts`, drain FIFO in order proxying each; on 240s → stop target, log `health timeout`, return `504` to queued, optionally fallback.
6. Every proxied/queued request emits one JSON log line (`request_id`, `hint`, `target_model`, `queue_depth`, `latency_ms`).

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `src/gateway/app.py` | Create | FastAPI app factory, lifespan, route registration |
| `src/gateway/proxy.py` | Create | `httpx.AsyncClient` streaming proxy, header/bearer passthrough, error propagation |
| `src/gateway/detector.py` | Create | `X-Model-Hint` + prefix detector, case-insensitive, default code |
| `src/gateway/queue.py` | Create | FIFO `asyncio.Queue`, 202/Retry-After, drain, 240s timeout |
| `src/gateway/metrics.py` | Create | `/health` + `/metrics` (Prometheus text), `nvidia-smi` VRAM |
| `src/orchestrator/registry.py` | Create | `config.yaml` loader, validator (`-np 1`, required fields), resolver |
| `src/orchestrator/lifecycle.py` | Create | `systemctl --user` wrapper, exclusive `asyncio.Lock`, stop-before-start |
| `src/orchestrator/health.py` | Create | Poll loop 2s, 180s nominal / 240s hard, `health_endpoint` per model |
| `src/orchestrator/vram.py` | Create | `nvidia-smi` query, feasibility check vs 24 GB, post-stop <2 GB assert |
| `src/orchestrator/idle.py` | Create | Background task, 10 min timeout, `idle_timeout` reason, reset on activity |
| `src/orchestrator/holds.py` | Create | Boot check `apt-mark hold` + `kornia==0.6.12` |
| `config/config.yaml` | Create | Model registry: `coder-q4-131k`, `coder-q5-65k`, `sdxl`, `wan` (future) |
| `systemd/user/*.service` | Modify | Verify `Restart=always`, add `ExecStart` with `-np 1` if missing |
| `tests/unit/test_registry.py` | Create | Registry validation, detector cases |
| `tests/integration/test_swap.py` | Create | systemctl + health + VRAM with mocks |
| `tests/e2e/test_queue.py` | Create | FIFO ordering, 202, timeout, latency < swap+5s |

## Interfaces / Contracts

### HTTP Endpoints

```
POST /v1/chat/completions      → proxy to llama-server :8082 (code)
POST /v1/embeddings            → proxy to llama-server :8082
GET  /v1/models                → proxy to active backend (or merged static list)
POST /v1/images/generations    → proxy to ComfyUI :8188 (image)
GET  /health                   → {"model": "sdxl"|null, "vram_used_mb": 6800,
                                  "queue_depth": 0, "status": "ready|swapping|idle"}
GET  /metrics                  → Prometheus text:
                                  model_router_queue_depth 2
                                  model_router_swaps_total 1
                                  model_router_vram_used_mb 6800
                                  model_router_health_poll_duration_seconds 112.4
```

All `/v1/*` preserve method, path, query, body, `Authorization: Bearer`, `Content-Type`, and `Accept`. Streaming (`stream:true`) returns `text/event-stream` chunked.

### Headers

- Request: `X-Model-Hint: code|image|video` (case-insensitive, highest priority). Absent → prefix scan.
- Response (success proxy): no extra header.
- Response (fallback): `X-Fallback: local-llm` + `fallback: true` semantics per `task-routing` spec.
- Response (queued): `Retry-After: <seconds>` + `202 {"queued":true,"target":"sdxl","retry_after":45}`.
- Response (timeout): `504 {"error":"model load timeout"}`.

### Registry Schema (config.yaml)

```yaml
models:
  coder-q4-131k:
    service: llama-code-q4.service
    port: 8082
    health_endpoint: /health
    vram_mb: 14000
    args: ["--model", "/models/coder-q4.gguf", "-np", "1", "--ctx-size", "131072"]
  coder-q5-65k:
    service: llama-code-q5.service
    port: 8082
    health_endpoint: /health
    vram_mb: 18000
    args: ["--model", "/models/coder-q5.gguf", "-np", "1", "--ctx-size", "65536"]
  sdxl:
    service: sdxl.service
    port: 8188
    health_endpoint: /health
    vram_mb: 6500
    args: ["--listen", "127.0.0.1", "--port", "8188"]
  # future
  wan:
    service: wan.service
    port: 8189
    health_endpoint: /health
    vram_mb: 12000
    args: ["--port", "8189"]

defaults:
  model: coder-q4-131k
  idle_timeout_seconds: 600
  swap_timeout_seconds: 240
  health_interval_seconds: 2
  bind: "127.0.0.1:8000"
```

Validation rules: every model requires `service`, `port`, `health_endpoint`, `vram_mb`; `args` must contain `-np 1` if llama-server; reject `-np >1`.

### Internal Types (Python)

```python
class ModelSpec(TypedDict):
    service: str
    port: int
    health_endpoint: str
    vram_mb: int
    args: list[str]

class HealthStatus(Literal["ready", "swapping", "idle"]): ...

class QueueItem(TypedDict):
    request_id: str
    target: str
    created_at: float
    scope: Request  # original ASGI scope for replay
```

## Persistence

- `config.yaml` — single source of truth for registry; edited by operator, hot-reloaded on `SIGHUP` (reread file, no restart).
- `systemd/user/*.service` — lifecycle persistence; `Restart=always` ensures `code` comes up on reboot (enabled unit). `systemctl --user is-active` is runtime state, not persisted by gateway.
- `nvidia-smi` — ephemeral VRAM truth; queried on every `/health` and before/after stop/start.
- Logs: structured JSON to `stdout` (journal) and `/tmp/model-router.log`, `/tmp/llama-*.log`, `/tmp/comfyui.log` per existing convention. Each request: `{"request_id","hint","target_model","queue_depth","latency_ms","routing"}`. Do NOT log `Authorization` value — redact to `Bearer ***`.
- No DB, no migration. Rollback: `systemctl --user stop model-router` + point clients to `8082/8188`.

## Security

- Bind `127.0.0.1:8000` only (never `0.0.0.0`). No external listener; reverse proxy (if ever needed) must add auth. `config.yaml` `bind` defaults to loopback and validator rejects non-loopback without explicit `allow_external: true`.
- Bearer passthrough only — gateway does not validate or store tokens; `Authorization` header forwarded verbatim, never logged.
- `nvidia-smi` output is local-only; `/health`/`/metrics` also bound to loopback, not exposed externally.
- No secret in `config.yaml`; model `args` must not contain API keys. Log redaction: `Authorization`, `X-Api-Key` scrubbed.
- `systemctl --user` runs as invoking user, not root; no `sudo`.
- Input validation: `X-Model-Hint` allowlist `code|image|video` (case-insensitive), else 400; prefix scan bounded to first 256 chars of `prompt` to avoid large-body DoS.

## Testing Strategy

| Layer | What to Test | Approach |
|-------|-------------|----------|
| Unit | `Registry` load/validate/resolve, `Detector` hint vs prefix vs default, `-np 1` enforcement, `holds` check parsing | `pytest` `tests/unit/test_registry.py`, `test_detector.py` — pure functions, no I/O, mocked `yaml.safe_load` |
| Integration | `Lifecycle` stop-before-start order, single lock coalescence, `Health` poll 2s/240s/timeout rollback, `VRAM` guard `<2GB`, `Idle` 10 min reaper reset | `tests/integration/test_swap.py` — mock `subprocess.run` for `systemctl`, mock `httpx` health, mock `nvidia-smi` output, assert call counts |
| E2E | FIFO ordering, `202 Retry-After`, drain latency `< swap+5s`, `504` on timeout, streaming passthrough, fallback `X-Fallback: local-llm`, full `code→image→video→code` sequence <180s each no OOM | `tests/e2e/test_queue.py` + manual `curl` harness — live gateway with stub backends on `8082/8188` (Python `http.server` returning SSE), concurrent `curl` with `X-Model-Hint`, assert queue depth metrics |

Coverage target: unit 90% for `registry`/`detector`; integration covers all `model-lifecycle` scenarios (including `unknown model`, `health timeout`, `VRAM block`); e2e covers `gateway-proxy` queue drain and `task-routing` fallback.

## Threat Matrix

| Boundary | Applicable | Reason / Safe-Failure Behavior | RED Test |
|----------|------------|-------------------------------|----------|
| Routing (`/v1/*` → backend port) | Applicable | Wrong port ⇒ proxy to dead backend, 502; must validate `Registry.port` on startup, reject unknown model 400 | Send `/v1/chat/completions` with `X-Model-Hint: video-unknown` → expect 400, no systemctl |
| Shell `systemctl` | Applicable | Injection via `service` name ⇒ `shell=False` + allowlist `^[a-z0-9-]+\.service$`, no interpolation | Config with `service: "foo; rm -rf /"` → validator rejects |
| Shell `nvidia-smi` | Applicable | Fixed command, no user input; parse failures → log warning, block swap (fail-closed) | Stub `nvidia-smi` returning garbage → `/health` reports `vram_used_mb: null`, swap blocked |
| Subprocess `health` poll | Applicable | SSRF via `health_endpoint` ⇒ restrict to `127.0.0.1:{port}{endpoint}` only, no external host | Config with `health_endpoint: http://evil.com` → validator rejects |
| VCS/PR automation | N/A | No PR automation in this change | — |
| Executable-file classification | N/A | No file upload execution | — |
| Process integration (systemd) | Applicable | Concurrent `start` OOM ⇒ exclusive `asyncio.Lock` + `stop` before `start`, `nvidia-smi` <2GB gate | 5 concurrent `image` requests → assert 1 `systemctl start` call count |

## Migration / Rollout

No migration required — greenfield, stateless. Rollout: `systemctl --user daemon-reload && systemctl --user enable --now model-router` → verify `/health` → point Open WebUI base URL to `http://127.0.0.1:8000`. Rollback: `systemctl --user stop model-router`; revert base URL to `8082/8188`. No data loss.

## Open Questions

- [ ] `vram_mb` for `wan` — stub 12000 pending measurement on 3090; needs real profiling before enabling video route.
- [ ] `GET /v1/models` merge — return static list from config or proxy to active backend only? Current design proxies to active; merging may benefit Open WebUI model picker.
- [ ] ComfyUI `health_endpoint` — ComfyUI has no native `/health`; propose `GET /system_stats` or TCP connect as readiness probe — confirm before finalizing config.

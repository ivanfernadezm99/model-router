# Proposal: GPU Model Router

## Intent

24 GB RTX 3090 cannot hold LLM 22 GB + SDXL 6.5 GB + video. Manual `systemctl` swaps are slow/racy. Gateway `:8000` owns VRAM exclusively — one model at a time, auto-swapped by task, FIFO queue for concurrency. Single OpenAI-compatible endpoint for Open WebUI/Claw/CLI; enables Wan/LTX without HW upgrade.

## Scope

### In Scope
- FastAPI `:8000` proxy `/v1/*` (OpenAI-compatible)
- Detector: `X-Model-Hint: code|image|video` or prefix `code:/image:/video:`; default `code`
- Orchestrator: `systemctl --user stop/start` + health poll 180s
- FIFO queue during swap (202 Retry-After)
- Idle release 10 min; `/health` + `/metrics` (nvidia-smi)
- systemd checks (Restart=always, holds)

### Out of Scope
- Training/quantization
- External API keys/billing
- Multi-GPU/distributed
- ComfyUI workflows (only lifecycle)
- RBAC beyond bearer passthrough

## Capabilities

### New Capabilities
- `gateway-proxy`: proxy, queuing, routing, metrics
- `model-lifecycle`: exclusive VRAM, systemctl, health, idle reclaim
- `task-routing`: header/prefix classification

### Modified Capabilities
- None — greenfield

## Approach

`:8000` sole entry. Detect → check active → mismatch: enqueue, stop current, start target, poll `/health` (90–180s), proxy. Single lock, FIFO, idle timer. Stack: FastAPI + httpx, systemctl, nvidia-smi. Config `config.yaml` (model→service→port→health→VRAM). JSON logs.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/gateway/` | New | Proxy, routing, queue, metrics |
| `src/orchestrator/` | New | systemctl, health, idle |
| `config/config.yaml` | New | Model registry |
| `systemd/user/*.service` | Modified | Verify Restart/holds |
| `openspec/specs/` | New | 3 new specs |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Load 90–180s | High | FIFO + 202; 240s timeout; pre-warm `code` |
| OOM `-np>1`/dual start | Med | Enforce `-np 1`; exclusive lock; VRAM check |
| Driver hold break | Low | `apt-mark hold`; boot check; manual fallback |
| FX-8350 no AVX2 (kornia) | High | Pin `kornia==0.6.12` |

## Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Ollama swap | LLM-only, no SDXL/video |
| All resident | 22+6.5+12>24 GB impossible |
| K8s/Triton | Overkill single-node; 10× ops |

## Rollback Plan

`systemctl --user stop model-router`; clients revert to 8082/8188; manual `systemctl` resumes. Stateless, no migration/data loss.

## Dependencies

CUDA 12.6/driver 580/nvidia-smi; systemd user; Python 3.11 FastAPI httpx; llama-server 8082, ComfyUI 8188.

## Success Criteria

- [ ] `:8000` routes code/image/video via header/prefix (default code)
- [ ] FIFO during swap, no drops, latency < swap+5s
- [ ] Idle 10 min → VRAM <2 GB; reload <180s
- [ ] `/health` shows model/VRAM/queue; reboot → `code`
- [ ] No OOM on code→image→video→code with `-np 1`

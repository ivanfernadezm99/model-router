# Tasks: GPU Model Router

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1050 |
| 400-line budget risk | High |
| 800-line budget risk | High — exceeds 800 |
| Chained PRs recommended | Yes |
| Suggested split | PR1→PR2→PR3→PR4 (≤280 each) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

> Forecast ~1050 exceeds 800 preflight. Auto-chain slices 4 PRs.

### Suggested Work Units

| U | Goal | PR | Test | Harness | Rollback |
|---|------|----|------|---------|----------|
|1|Registry/config/detector|PR1|`pytest tests/unit -v`|`registry --validate config.yaml`|Remove config/registry/detector|
|2|Orchestrator lock/health/VRAM/idle|PR2|`pytest tests/integration -v`|`systemctl status + nvidia-smi stub`|Revert orchestrator/|
|3|Gateway queue/proxy/app|PR3|`pytest tests/e2e -k fifo`|`curl -H X-Model-Hint localhost:8000`|Revert gateway/|
|4|Metrics/threats/docs|PR4|`pytest tests/ -q`| `curl /health && /metrics`|Revert metrics/docs|

## Phase 1: Foundation

- [ ] 1.1 `config/config.yaml` (~40 P0) — 4 models + defaults. Test: `yaml.safe_load`.
- [ ] 1.2 `src/orchestrator/registry.py` validate/resolve allowlist (~90 P0) — unknown→400 no systemctl. Test: `test_unknown_model`.
- [ ] 1.3 `-np 1` enforcement (~30 P0) dep 1.2 — reject `-np>1`. RED `test_np_rejected`.
- [ ] 1.4 `src/orchestrator/holds.py` hold checks block swap (~50 P1) dep 1.2 — warning if missing. Test: mock subprocess.

## Phase 2: Orchestrator

- [ ] 2.1 `src/orchestrator/vram.py` nvidia-smi fail-closed null (~60 P0) — garbage→block. RED `test_vram_garbage`.
- [ ] 2.2 `src/orchestrator/health.py` poll 2s 180s/240s→504 (~70 P0) dep 2.1 — rollback stop. Test: `test_health_timeout`.
- [ ] 2.3 `src/orchestrator/lifecycle.py` Lock stop+<2048 coalesce 5→1 (~90 P0) dep 2.2 — shell=False. RED `test_lock_coalescence`.
- [ ] 2.4 `src/orchestrator/idle.py` 60s tick 600s→idle reset (~60 P1) dep 2.3 — status idle. Test: `test_idle`.

## Phase 3: Gateway

- [ ] 3.1 `src/gateway/detector.py` header/prefix default code (~60 P0) dep 1.2 — case-insensitive. Test: `test_detector` 5 cases.
- [ ] 3.2 `src/gateway/queue.py` FIFO 202 Retry-After drain <swap+5s (~80 P0) dep 2.3/3.1 — 240s→504. Test: `test_fifo_drain`.
- [ ] 3.3 `src/gateway/proxy.py` httpx stream redact Bearer (~90 P0) dep 3.2 — 500 propagate. Test: SSE `aiter_bytes`.
- [ ] 3.4 `src/gateway/metrics.py` /health + /metrics Prometheus (~50 P1) dep 2.1/3.2 — queue/vram counters. Test: `curl /metrics`.
- [ ] 3.5 `src/gateway/app.py` FastAPI :8000 lifespan JSON log (~80 P0) dep 3.x — reject external bind. Test: `test_bind`.

## Phase 4: Testing

- [ ] 4.1 RED `tests/unit/test_threats.py` 5 boundaries (~100 P0) dep 1.2/2.3 — injection/SSRF/lock. Test: `-k threat` RED→GREEN.
- [ ] 4.2 `tests/integration/test_swap.py` stop/health/VRAM/fallback X-Fallback (~120 P1) dep 2.x/3.x — 502 if code down.
- [ ] 4.3 `tests/e2e/test_queue.py` FIFO/stream <swap+5s seq <180s (~120 P1) dep 3.x — stub :8082/:8188 SSE.

## Phase 5: Systemd & Docs

- [ ] 5.1 `systemd/user/*.service` Restart=always -np 1 (~20 P1) dep 1.3 — enabled boot. Test: `grep Restart`.
- [ ] 5.2 `README.md` rollback+SIGHUP+coverage 90% (~30 P2) dep 5.1 — `pytest --cov 90`.

## Dependencies & Estimation

| T | Dep | L | P |
|---|-----|---|---|
|1.1|—|40|P0|
|1.2|1.1|90|P0|
|1.3|1.2|30|P0|
|1.4|1.2|50|P1|
|2.1|—|60|P0|
|2.2|2.1|70|P0|
|2.3|2.2|90|P0|
|2.4|2.3|60|P1|
|3.1|1.2|60|P0|
|3.2|2.3/3.1|80|P0|
|3.3|3.2|90|P0|
|3.4|2.1/3.2|50|P1|
|3.5|3.x|80|P0|
|4.1|1.2/2.3|100|P0|
|4.2|2.x/3.x|120|P1|
|4.3|3.x|120|P1|
|5.1|1.3|20|P1|
|5.2|5.1|30|P2|

Total 18 tasks ~1050 lines. Each PR ≤280. Global accept: routing FIFO lock metrics -np1 180s no OOM.

# Model Lifecycle Specification

## Purpose

Exclusive VRAM ownership for one model at a time. Defines the model registry, systemd lifecycle, health gating, and protections against OOM, driver drift, and dependency breakage on a single 24 GB RTX 3090.

## Requirements

### Requirement: Model Registry Definition

The system MUST define each model in `config.yaml` with `service` (systemd unit), `port`, `health_endpoint`, `vram_mb`, and `args`, covering at least `coder-q4-131k`, `coder-q5-65k`, and `sdxl`.

#### Scenario: Registry resolves model to service

- GIVEN `config.yaml` maps `coder-q4-131k` → `llama-code-q4.service` on `:8082`
- WHEN the orchestrator resolves `coder-q4-131k`
- THEN it returns service `llama-code-q4.service`, port `8082`, and `vram_mb` value from config

#### Scenario: Unknown model rejected

- GIVEN a request references model `video-unknown`
- WHEN the orchestrator looks it up
- THEN it returns an error `unknown model` without invoking systemctl

### Requirement: Exclusive Systemd Lifecycle

The system MUST use `systemctl --user stop/start` to enforce one active model, MUST run with `-np 1` (single slot), and systemd units MUST declare `Restart=always`.

#### Scenario: Exclusive stop-before-start

- GIVEN `coder-q4-131k` is active
- WHEN a switch to `sdxl` is requested
- THEN the orchestrator runs `systemctl --user stop llama-code-q4.service` and waits for exit before `start sdxl.service`

#### Scenario: Single-slot enforcement

- GIVEN any model start command is issued
- WHEN the orchestrator builds the exec args
- THEN the args include `-np 1` and the system rejects configs with `-np >1`

#### Scenario: Hold protection

- GIVEN packages `cuda-driver-580` and `kornia==0.6.12` are pinned
- WHEN the orchestrator runs its boot check
- THEN it verifies `apt-mark hold` is active and `pip freeze | grep kornia==0.6.12` succeeds; otherwise it logs a warning and blocks swap

### Requirement: Health Polling and Readiness Gate

The system MUST poll the target `health_endpoint` after start, consider the model ready only on HTTP 200, and timeout after 240s; a switch MUST complete within 180s under nominal load.

#### Scenario: Successful health poll within budget

- GIVEN `sdxl` was started on `:8188` with `health_endpoint: /health`
- WHEN the orchestrator polls every 2s
- THEN it returns `ready` within 90–180s on first `200` and allows traffic

#### Scenario: Health timeout triggers rollback

- GIVEN the target does not return `200` within 240s
- WHEN the timeout expires
- THEN the orchestrator stops the target, logs `health timeout`, and returns `504` to queued callers

### Requirement: Idle Reclaim and VRAM Enforcement

The system MUST reclaim VRAM after 10 min idle (stop active service until `<2 GB` via `nvidia-smi`) and MUST verify VRAM feasibility before starting a model that would exceed 24 GB.

#### Scenario: Idle 10 min releases VRAM

- GIVEN `coder-q5-65k` has had no requests for 10 min
- WHEN the idle timer fires
- THEN the orchestrator stops the service and `nvidia-smi` reports `<2048 MB` used

#### Scenario: VRAM check blocks oversized start

- GIVEN `config.yaml` declares `sdxl` needs 6500 MB and current `nvidia-smi` shows 22 GB resident
- WHEN a switch to `sdxl` is requested without stopping current
- THEN the orchestrator refuses to start until the exclusive lock frees VRAM

### Requirement: Switch Time Budget

The system MUST complete any `code → image → video → code` switch sequence with each individual switch <180s and without OOM when `-np 1` and exclusive lock are enforced.

#### Scenario: Sequential switches under budget

- GIVEN the sequence `coder-q4` → `sdxl` → `wan` → `coder-q4`
- WHEN each switch is measured from `stop` to first `200` health
- THEN each duration is `<180s` and no `CUDA OOM` appears in logs

# Gateway Proxy Specification

## Purpose

Single OpenAI-compatible entry point on `:8000` that proxies all `/v1/*` traffic, queues requests during model swaps, and exposes operational metrics. Owns no model logic — delegates lifecycle to the orchestrator.

## Requirements

### Requirement: OpenAI-Compatible Proxy Endpoint

The system MUST expose an OpenAI-compatible HTTP proxy on `:8000` that forwards all `/v1/*` requests (chat/completions, embeddings, models) to the active backend and preserves headers, streaming, and bearer passthrough.

#### Scenario: Proxy chat completion to active backend

- GIVEN the gateway is running and `code` model is active on `:8082`
- WHEN a client POSTs to `/v1/chat/completions` with a valid bearer token
- THEN the gateway proxies the request to `http://localhost:8082/v1/chat/completions` and returns the backend response verbatim

#### Scenario: Streaming response passthrough

- GIVEN a client sends `stream: true` to `/v1/chat/completions`
- WHEN the backend returns `text/event-stream` chunks
- THEN the gateway streams chunks to the client without buffering the full body

#### Scenario: Bearer token passthrough

- GIVEN a request includes `Authorization: Bearer <token>`
- WHEN the gateway proxies the request
- THEN it forwards the header unchanged and MUST NOT require separate RBAC

### Requirement: FIFO Queue During Model Swap

The system MUST queue concurrent requests FIFO while a model switch is in progress, return `202 Accepted` with `Retry-After` to queued callers, and process the queue in order without dropping requests.

#### Scenario: Queue second request during swap

- GIVEN a swap `code` → `image` is in progress (health poll active)
- WHEN a second request arrives requiring `image`
- THEN the gateway returns `202 Accepted` with `Retry-After: <seconds>` and enqueues the request FIFO

#### Scenario: Drain queue after swap completes

- GIVEN 3 requests are queued FIFO during a swap
- WHEN the target model becomes healthy
- THEN the gateway dequeues and proxies them in arrival order with total added latency < 5s beyond swap time

#### Scenario: Queue timeout exceeded

- GIVEN a request has waited 240s in the queue
- WHEN the target model is still not healthy
- THEN the gateway returns `504 Gateway Timeout` with JSON `{"error":"model load timeout"}`

### Requirement: Routing Delegation and Metrics Exposure

The system MUST delegate task-to-model resolution to the task-routing capability and expose `/health` and `/metrics` endpoints that report active model, VRAM usage via `nvidia-smi`, and queue depth.

#### Scenario: Health reflects active model and VRAM

- GIVEN `sdxl` is active and `nvidia-smi` reports 6.8 GB used
- WHEN a client GETs `/health`
- THEN the response is `200` with `{"model":"sdxl","vram_used_mb":6800,"queue_depth":0,"status":"ready"}`

#### Scenario: Metrics expose queue and swap counters

- GIVEN one swap has occurred and queue depth is 2
- WHEN a client GETs `/metrics`
- THEN the response includes `model_router_queue_depth 2` and `model_router_swaps_total 1`

### Requirement: Timeout and Error Propagation

The system MUST enforce a 240s swap timeout, propagate backend errors transparently, and log every proxy decision as structured JSON.

#### Scenario: Backend 500 propagates unchanged

- GIVEN the active backend returns `500` with body `{"error":"oom"}`
- WHEN the gateway receives the response
- THEN it forwards status `500` and body unchanged to the client

#### Scenario: JSON log per request

- GIVEN any `/v1/*` request is proxied or queued
- WHEN the gateway handles it
- THEN it emits one JSON log line with `request_id`, `hint`, `target_model`, `queue_depth`, and `latency_ms`

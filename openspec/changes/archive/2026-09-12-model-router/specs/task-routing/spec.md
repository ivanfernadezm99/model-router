# Task Routing Specification

## Purpose

Deterministic classification of every request to `code|image|video` using explicit hints or body prefixes, with default fallback, idle management, and exclusive execution guarantees.

## Requirements

### Requirement: Task Classification via Hint or Prefix

The system MUST classify each request by `X-Model-Hint: code|image|video` header (highest priority) or, if absent, by body prefix `code:` / `image:` / `video:`; header and prefix values MUST be case-insensitive.

#### Scenario: Header hint wins over body prefix

- GIVEN a request has `X-Model-Hint: image` and body starts with `code: hello`
- WHEN the detector classifies it
- THEN the result is `image`

#### Scenario: Prefix detection when header absent

- GIVEN no `X-Model-Hint` header and body is `video: animate a cat`
- WHEN the detector classifies it
- THEN the result is `video`

#### Scenario: Case-insensitive hint

- GIVEN header `X-Model-Hint: CoDe`
- WHEN classified
- THEN the result is `code`

### Requirement: Default Task Fallback

The system MUST default to `code` when neither header nor prefix is present and MUST log the fallback decision.

#### Scenario: Default to code with no hint

- GIVEN a plain `POST /v1/chat/completions` with body `{"prompt":"hello"}` and no hint or prefix
- WHEN classified
- THEN the result is `code` and a JSON log includes `"routing":"default:code"`

### Requirement: Idle Release After Inactivity

The system MUST release the active model after 10 min without proxied requests and MUST record the release reason as `idle_timeout`.

#### Scenario: Idle timer releases model

- GIVEN `sdxl` is active and the last request was 10 min ago
- WHEN the idle timer fires
- THEN the orchestrator stops `sdxl` and `/health` reports `{"model":null,"status":"idle"}`

#### Scenario: Activity resets idle timer

- GIVEN the idle timer is at 9 min
- WHEN a new request is proxied
- THEN the timer resets to 0 and no release occurs

### Requirement: Exclusive VRAM Lock

The system MUST hold a single exclusive lock during any swap so that only one model occupies VRAM, and concurrent requests for the same target MUST coalesce to a single `systemctl start`.

#### Scenario: Concurrent same-target coalesces

- GIVEN a swap to `image` is in progress
- WHEN 5 concurrent requests all require `image`
- THEN only one `systemctl start` is issued and all 5 are queued FIFO behind the same swap

#### Scenario: Cross-task request waits on lock

- GIVEN a swap to `video` holds the lock
- WHEN a `code` request arrives
- THEN it waits FIFO and does not interrupt the in-progress swap

### Requirement: Fallback to Local LLM

The system SHOULD fall back to the local LLM (`code` on `:8082`) when the target image/video backend is unavailable after timeout, and MUST include `fallback: true` in the response header when doing so.

#### Scenario: Fallback when image backend times out

- GIVEN a switch to `sdxl` timed out after 240s
- WHEN the gateway handles the queued request
- THEN it proxies to the local `code` backend and returns `X-Fallback: local-llm` in the response

#### Scenario: No fallback when code itself fails

- GIVEN the fallback `code` backend is also unhealthy
- WHEN fallback is attempted
- THEN the gateway returns `502 Bad Gateway` without infinite retry

"""Tests for LoopGuardMiddleware + LoopGuard semantics.

Regression coverage for the 2026-09-28 front outage: the middleware counted
EVERY http request (health polls, web UI, jobs) against the agent iteration
budget and 429'd the whole gateway after ~12 requests. Operational traffic
must bypass the guard; only /v1/ inference is inspected.
"""

import json

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.orchestrator.loop_guard import (
    LoopGuard,
    ToolErrorType,
    get_session_guard,
    map_http_status_to_error,
)
from src.orchestrator.loop_middleware import LoopGuardMiddleware


def _make_request(path: str, body: bytes = b"", headers=None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def _ok_next(request: Request):
    return JSONResponse({"ok": True})


def _chat_body(text: str) -> bytes:
    return json.dumps(
        {"model": "local-template", "messages": [{"role": "user", "content": text}]}
    ).encode()


@pytest.mark.asyncio
async def test_non_v1_traffic_bypasses_guard():
    """Health/metrics/jobs/web polling must never be 429'd, no matter how many."""
    mw = LoopGuardMiddleware(app=None)
    for _ in range(30):
        for path in ("/health", "/metrics", "/jobs/models/list", "/api/queue"):
            resp = await mw.dispatch(_make_request(path), _ok_next)
            assert resp.status_code == 200, f"{path} blocked"


@pytest.mark.asyncio
async def test_v1_identical_prompt_blocked_on_third_try():
    """Same inference prompt 3x in a row -> 429 LOOP_DETECTED, retryable=false."""
    mw = LoopGuardMiddleware(app=None)
    body = _chat_body("read the skill file again")

    r1 = await mw.dispatch(_make_request("/v1/chat/completions", body), _ok_next)
    r2 = await mw.dispatch(_make_request("/v1/chat/completions", body), _ok_next)
    assert r1.status_code == 200
    assert r2.status_code == 200

    r3 = await mw.dispatch(_make_request("/v1/chat/completions", body), _ok_next)
    assert r3.status_code == 429


@pytest.mark.asyncio
async def test_v1_different_prompts_pass_through():
    """Distinct prompts are independent loop candidates, not a loop."""
    mw = LoopGuardMiddleware(app=None)
    for i in range(5):
        resp = await mw.dispatch(
            _make_request("/v1/chat/completions", _chat_body(f"prompt number {i}")),
            _ok_next,
        )
        assert resp.status_code == 200


def test_semantically_identical_errors_stop():
    """'File not found' then 'Could not locate' -> second is non-retryable STOP."""
    guard = LoopGuard(session_id="test-semantic")
    first = guard.record_failure("File not found: /foo/bar", "read_file")
    assert first.error_type == ToolErrorType.FILE_NOT_FOUND
    assert first.retryable is False

    second = guard.record_failure("Could not locate /foo/bar", "read_file")
    assert second.error_type == ToolErrorType.FILE_NOT_FOUND
    assert second.retryable is False
    assert "retryable=false" in second.message


def test_http_status_mapping_order():
    """502/503 are network errors, not generic 5xx model errors."""
    assert map_http_status_to_error(502) == ToolErrorType.NETWORK_ERROR
    assert map_http_status_to_error(503) == ToolErrorType.NETWORK_ERROR
    assert map_http_status_to_error(500) == ToolErrorType.MODEL_ERROR
    assert map_http_status_to_error(404) == ToolErrorType.FILE_NOT_FOUND
    assert map_http_status_to_error(403) == ToolErrorType.PERMISSION_DENIED


def test_shared_session_guard_accumulates():
    """Same session id returns the same guard (limits accumulate, not reset)."""
    g1 = get_session_guard(session_id="test-shared-accumulates")
    g2 = get_session_guard(session_id="test-shared-accumulates")
    assert g1 is g2

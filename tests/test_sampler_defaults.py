"""Regression tests for the gateway sampler defaults in src/gateway/proxy.py.

The proxy used to unconditionally rewrite the client's sampling on every /v1
request. That killed the runaway loop it was built for, but it also penalised
repetition — and a file path is repetition. Agent tool calls lost paths because
the sampler fought the model on the one task that needs precision.

Two rules under test:
  1. a value the client set is never overridden
  2. agent traffic (request carries `tools`) gets repetition-friendly defaults

Run: python3 -m pytest tests/test_sampler_defaults.py -q
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from src.gateway.proxy import proxy_request

PATH = "/home/servidor/Descargas/model-router/src/gateway/proxy.py"


def _request(payload: dict) -> Request:
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def _sent_body(payload: dict) -> dict:
    """Run one proxied request and return the JSON body actually forwarded."""
    captured = {}

    async def _send(self, req, **kwargs):
        captured["content"] = req.content
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.aread = AsyncMock(return_value=b'{"ok":true}')
        resp.aclose = AsyncMock()
        return resp

    with patch("httpx.AsyncClient.send", _send):
        await proxy_request(_request(payload), 8082, task="code")

    assert "content" in captured, "request never reached the backend"
    return json.loads(captured["content"])


AGENT = {"messages": [{"role": "user", "content": f"lee {PATH}"}], "tools": [{"type": "function", "function": {"name": "read"}}]}
PLAIN_CODE = {"messages": [{"role": "user", "content": "def suma(a, b):\n    return a + b\n"}]}


# --- rule 1: never override the client ---


@pytest.mark.parametrize("key,value", [("temperature", 0.9), ("top_p", 0.95), ("top_k", 40), ("repeat_penalty", 1.0), ("dry_multiplier", 0.7)])
def test_client_sampling_is_never_overridden(key, value):
    out = asyncio_run(_sent_body({**PLAIN_CODE, key: value}))
    assert out[key] == value, f"{key} was overwritten to {out[key]}"


def test_client_explicit_top_p_high_survives():
    """The old code capped any top_p above 0.9 to 0.85."""
    out = asyncio_run(_sent_body({**PLAIN_CODE, "top_p": 0.95}))
    assert out["top_p"] == 0.95


def test_client_explicit_temperature_high_survives():
    out = asyncio_run(_sent_body({**PLAIN_CODE, "temperature": 0.8}))
    assert out["temperature"] == 0.8


def test_client_dry_allowed_length_survives_on_code():
    out = asyncio_run(_sent_body({**PLAIN_CODE, "dry_multiplier": 0.9, "dry_allowed_length": 6}))
    assert out["dry_allowed_length"] == 6


# --- rule 2: agent traffic is repetition-friendly ---


def test_agent_traffic_gets_no_dry():
    """DRY with allowed_length=2 is what destroyed paths in tool calls."""
    out = asyncio_run(_sent_body(AGENT))
    assert "dry_multiplier" not in out
    assert "dry_allowed_length" not in out


def test_agent_traffic_gets_mild_repeat_penalty():
    out = asyncio_run(_sent_body(AGENT))
    assert out["repeat_penalty"] == 1.05


def test_agent_traffic_does_not_get_forced_top_k():
    """top_k truncates the tail; a long path needs that tail."""
    out = asyncio_run(_sent_body(AGENT))
    assert "top_k" not in out


def test_agent_traffic_keeps_its_own_dry():
    out = asyncio_run(_sent_body({**AGENT, "dry_multiplier": 0.8, "dry_allowed_length": 4}))
    assert out["dry_multiplier"] == 0.8
    assert out["dry_allowed_length"] == 4


# --- the anti-loop defaults still exist where they belong ---


def test_plain_code_still_gets_aggressive_dry():
    """The loop this was built for came from plain code, not tool calls."""
    out = asyncio_run(_sent_body(PLAIN_CODE))
    assert out["dry_multiplier"] == 0.9
    assert out["dry_allowed_length"] == 2


def test_plain_code_still_gets_determinism():
    out = asyncio_run(_sent_body(PLAIN_CODE))
    assert out["temperature"] == 0.2
    assert out["top_p"] == 0.85
    assert out["top_k"] == 20
    assert out["repeat_penalty"] == 1.15


def test_prose_gets_no_dry_forced_on():
    """Old code set dry_multiplier=0 on prose, overriding a client that wanted it."""
    out = asyncio_run(_sent_body({"messages": [{"role": "user", "content": "Escribime un mail corto para un cliente."}]}))
    assert "dry_multiplier" not in out
    assert out["temperature"] == 0.6


def test_prose_with_client_dry_is_softened_not_killed():
    out = asyncio_run(_sent_body({"messages": [{"role": "user", "content": "Escribime un mail corto para un cliente."}], "dry_multiplier": 0.9, "dry_allowed_length": 2}))
    assert out["dry_multiplier"] == 0.9
    assert out["dry_allowed_length"] == 8


# --- payload passthrough ---


def test_messages_and_tools_reach_backend_untouched():
    out = asyncio_run(_sent_body(AGENT))
    assert out["messages"] == AGENT["messages"]
    assert out["tools"] == AGENT["tools"]


def test_path_in_prompt_is_not_mutated():
    out = asyncio_run(_sent_body(AGENT))
    assert PATH in out["messages"][0]["content"]


# --- kill switch ---


def test_sampler_defaults_disabled_by_env():
    import os

    os.environ["SAMPLER_DEFAULTS_DISABLED"] = "1"
    try:
        out = asyncio_run(_sent_body(PLAIN_CODE))
    finally:
        del os.environ["SAMPLER_DEFAULTS_DISABLED"]
    assert "dry_multiplier" not in out
    assert "top_k" not in out


# --- non-chat traffic untouched ---


def test_embeddings_style_payload_untouched():
    payload = {"input": "hola", "model": "x", "temperature": 0.7}
    out = asyncio_run(_sent_body(payload))
    assert out == payload


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)

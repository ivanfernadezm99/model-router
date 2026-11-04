"""GATEWAY_API_KEY middleware: optional, but a supplied Bearer must match.

The 9router dashboard runs on http://localhost:20128 and validates this OpenAI
endpoint from the browser, so a rejected key must still be readable (CORS
headers present) instead of surfacing as an opaque network error.
"""

import pytest
from fastapi.testclient import TestClient

import src.gateway.app as gateway_app
from src.gateway.app import app

ORIGIN = "http://localhost:20128"


def _raise_unknown_hint(*_args, **_kwargs):
    """Short-circuit the proxy so tests never touch systemctl or a model."""
    raise ValueError("unknown hint")


def test_wrong_bearer_gets_401_readable_from_the_browser(monkeypatch):
    monkeypatch.setattr(gateway_app, "GATEWAY_API_KEY", "s3cret")
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong", "Origin": ORIGIN},
        json={"prompt": "hi"},
    )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid api key"}
    assert response.headers.get("access-control-allow-origin") == ORIGIN


def test_missing_authorization_is_allowed_when_a_key_is_configured(monkeypatch):
    monkeypatch.setattr(gateway_app, "GATEWAY_API_KEY", "s3cret")
    monkeypatch.setattr(gateway_app, "detect_task", _raise_unknown_hint)
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Origin": ORIGIN},
        json={"prompt": "hi"},
    )

    # Existing keyless clients keep working; the request reaches the route and
    # fails later on the (patched) detector, never on authentication.
    assert response.status_code != 401


def test_no_key_configured_never_rejects(monkeypatch):
    monkeypatch.setattr(gateway_app, "GATEWAY_API_KEY", "")
    monkeypatch.setattr(gateway_app, "detect_task", _raise_unknown_hint)
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer anything", "Origin": ORIGIN},
        json={"prompt": "hi"},
    )

    assert response.status_code != 401

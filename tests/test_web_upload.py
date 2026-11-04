import io

import pytest
from types import SimpleNamespace

from web.app import app


def test_upload_to_composer_uses_env_credential_and_preserves_multipart_file(monkeypatch):
    service_token = "configured-service-token"
    monkeypatch.setenv("COMPOSER_SERVICE_TOKEN", service_token)
    monkeypatch.setenv("COMPOSER_BACKEND_API", "https://pos-backend.example.test/api")
    captured = {}

    def fake_post(url, *, headers, files, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["files"] = files
        captured["timeout"] = timeout
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "isSuccess": True,
                "data": {"publicUrl": "https://media.example.test/generated.png"},
            },
        )

    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        response = client.post(
            "/generacion/upload-to-composer",
            data={"file": (io.BytesIO(b"generated-image-bytes"), "generated.png", "image/png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert response.get_json() == {"publicUrl": "https://media.example.test/generated.png"}
    assert captured["url"] == "https://pos-backend.example.test/api/Zernio/media/upload"
    assert captured["headers"] == {"Authorization": f"Bearer {service_token}"}
    assert captured["files"]["file"] == ("generated.png", b"generated-image-bytes", "image/png")


def test_upload_to_composer_prefers_explicit_user_bearer(monkeypatch):
    monkeypatch.setenv("COMPOSER_SERVICE_TOKEN", "server-service-credential")
    captured = {}

    def fake_post(_url, *, headers, files, timeout):
        captured.update(headers=headers, files=files)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"isSuccess": True, "data": {"publicUrl": "https://media.example.test/user.png"}},
        )

    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        response = client.post(
            "/generacion/upload-to-composer",
            headers={"Authorization": "Bearer user-session-token"},
            data={"file": (io.BytesIO(b"user-upload"), "user.png", "image/png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert captured["headers"] == {"Authorization": "Bearer user-session-token"}
    assert captured["files"]["file"] == ("user.png", b"user-upload", "image/png")


def test_upload_to_composer_rejects_missing_or_non_file_multipart_field(monkeypatch):
    monkeypatch.setenv("COMPOSER_SERVICE_TOKEN", "configured-service-token")
    fake_post = lambda *args, **kwargs: pytest.fail("backend must not be called without an uploaded file")
    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        missing = client.post("/generacion/upload-to-composer")
        text_field = client.post(
            "/generacion/upload-to-composer",
            data={"file": "not-a-file"},
            content_type="multipart/form-data",
        )

    assert missing.status_code == 400
    assert missing.get_json() == {"error": "falta file"}
    assert text_field.status_code == 400
    assert text_field.get_json() == {"error": "falta file"}


def test_upload_to_composer_requires_server_credential_when_no_user_jwt(monkeypatch):
    monkeypatch.delenv("COMPOSER_SERVICE_TOKEN", raising=False)
    fake_post = lambda *args, **kwargs: pytest.fail("backend must not be called without credentials")
    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        response = client.post(
            "/generacion/upload-to-composer",
            data={"file": (io.BytesIO(b"generated-image-bytes"), "generated.png", "image/png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "no hay credencial de subida configurada en el servidor"
    }

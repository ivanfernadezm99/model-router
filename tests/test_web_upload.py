import io

import pytest
from types import SimpleNamespace

from web.app import app


def test_upload_to_composer_uses_user_jwt_and_preserves_multipart_file(monkeypatch):
    """User's JWT from Authorization header is used for ECFileManager upload."""
    captured = {}

    def fake_post(url, *, headers, files, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["files"] = {}
        for k, v in files.items():
            if isinstance(v, tuple) and len(v) >= 3:
                name, content, mime = v[0], v[1], v[2]
                if hasattr(content, "read"):
                    content.seek(0)
                    content_bytes = content.read()
                else:
                    content_bytes = content
                captured["files"][k] = (name, content_bytes, mime)
            else:
                captured["files"][k] = v
        captured["data"] = data
        captured["timeout"] = timeout
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"id": "file-123"},
        )

    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        response = client.post(
            "/generacion/upload-to-composer",
            headers={"Authorization": "Bearer user-session-token"},
            data={"file": (io.BytesIO(b"generated-image-bytes"), "generated.png", "image/png")},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    assert response.get_json() == {"publicUrl": "https://ecfilemanager.duckdns.org/api/file/file-123/download"}
    assert captured["url"] == "https://ecfilemanager.duckdns.org/api/file/application/061c9c31-3550-41d8-821c-631a6df26a1b"
    assert captured["headers"] == {"Authorization": "Bearer user-session-token"}
    assert captured["files"]["file"][0] == "generated.png"
    assert captured["files"]["file"][1] == b"generated-image-bytes"
    assert captured["files"]["file"][2] == "image/png"
    assert captured["data"] == {"isTemporary": "false", "description": ""}


def test_upload_to_composer_rejects_missing_or_invalid_auth_header(monkeypatch):
    """Missing or invalid Authorization header returns 401."""
    fake_post = lambda *args, **kwargs: pytest.fail("backend must not be called without auth")
    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        # No Authorization header
        missing = client.post(
            "/generacion/upload-to-composer",
            data={"file": (io.BytesIO(b"generated-image-bytes"), "generated.png", "image/png")},
            content_type="multipart/form-data",
        )
        # Invalid Authorization header
        invalid = client.post(
            "/generacion/upload-to-composer",
            headers={"Authorization": "InvalidToken"},
            data={"file": (io.BytesIO(b"generated-image-bytes"), "generated.png", "image/png")},
            content_type="multipart/form-data",
        )

    assert missing.status_code == 401
    assert missing.get_json() == {"error": "credencial de autorización faltante o inválida"}
    assert invalid.status_code == 401
    assert invalid.get_json() == {"error": "credencial de autorización faltante o inválida"}


def test_upload_to_composer_rejects_missing_or_non_file_multipart_field(monkeypatch):
    fake_post = lambda *args, **kwargs: pytest.fail("backend must not be called without an uploaded file")
    monkeypatch.setattr("web.app.requests.post", fake_post)

    with app.test_client() as client:
        missing = client.post(
            "/generacion/upload-to-composer",
            headers={"Authorization": "Bearer valid-token"},
        )
        text_field = client.post(
            "/generacion/upload-to-composer",
            headers={"Authorization": "Bearer valid-token"},
            data={"file": "not-a-file"},
            content_type="multipart/form-data",
        )

    assert missing.status_code == 400
    assert missing.get_json() == {"error": "falta file"}
    assert text_field.status_code == 400
    assert text_field.get_json() == {"error": "falta file"}

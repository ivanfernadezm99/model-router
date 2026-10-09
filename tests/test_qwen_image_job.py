"""Qwen-Image worker path — image jobs generate fixed photos via ComfyUI."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.jobs import worker as worker_mod


def _mock_comfy_client(tmp_path):
    """AsyncClient mock: /prompt -> id, /history -> 1 png, /view -> bytes."""
    png = b"\x89PNG\r\nfakepng"

    def _resp(payload=None, content=b""):
        m = MagicMock()
        m.json.return_value = payload or {}
        m.content = content
        m.raise_for_status = MagicMock()
        return m

    client = AsyncMock()
    prompt_id = "pid-123"
    client.post.return_value = _resp({"prompt_id": prompt_id})
    history_empty = {"empty": True}
    history_done = {prompt_id: {"outputs": {"10": {"images": [
        {"filename": "router-image_00001_.png", "subfolder": "", "type": "output"}]}}}}
    client.get.side_effect = [
        _resp(history_empty),  # first poll: nothing yet
        _resp(history_done),   # second poll: image ready
        _resp(content=png),    # /view bytes
    ]
    client.__aenter__.return_value = client
    return client, png


def test_target_for_kind_image_routes_qwen_image():
    assert worker_mod._target_for_kind("image") == "qwen-image"
    assert worker_mod._target_for_kind("video") == "wan-14b"


def test_qwen_image_workflow_has_fixed_nodes():
    wf = worker_mod._qwen_image_workflow("un gato", 512, 512, 20, 5.0, 42)
    assert wf["1"]["inputs"]["unet_name"] == "qwen-image-Q4_K_M.gguf"
    assert wf["2"]["inputs"]["type"] == "qwen_image"
    assert wf["4"]["inputs"]["text"] == "un gato"
    assert wf["6"]["inputs"]["width"] == 512
    assert wf["8"]["inputs"]["seed"] == 42
    assert wf["8"]["inputs"]["steps"] == 20


def test_run_comfyui_t2i_downloads_file(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "OUTPUT_DIR", str(tmp_path))
    client, png = _mock_comfy_client(tmp_path)
    with patch.object(worker_mod.httpx, "AsyncClient", return_value=client):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            out = asyncio.run(worker_mod._run_comfyui_t2i(
                8188, {"prompt": "un gato", "width": 512, "height": 512, "steps": 20, "cfg": 5.0}, "job123"))
    assert out["file"] == str(tmp_path / "job123.png")
    assert open(out["file"], "rb").read() == png
    # workflow posted to /prompt carries the prompt
    posted = client.post.call_args
    assert "un gato" in json_text(posted)


def json_text(call_args):
    args, kwargs = call_args
    assert args[0].endswith("/prompt")
    return kwargs["json"]["prompt"]["4"]["inputs"]["text"]


def test_run_comfyui_t2i_timeout_reports_backend_error(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(worker_mod, "COMFY_POLL_TIMEOUT_S", 0)
    client = AsyncMock()

    def _resp(payload=None, content=b""):
        m = MagicMock()
        m.json.return_value = payload or {}
        m.content = content
        m.raise_for_status = MagicMock()
        return m

    client.post.return_value = _resp({"prompt_id": "pid-x"})
    client.get.return_value = _resp({"other": True})  # never the image
    client.__aenter__.return_value = client
    with patch.object(worker_mod.httpx, "AsyncClient", return_value=client):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            out = asyncio.run(worker_mod._run_comfyui_t2i(
                8188, {"prompt": "x"}, "jobX"))
    assert "backend_error" in out and "timeout" in out["backend_error"]


def test_apply_text_overlays_exact_and_sanitized(tmp_path):
    from PIL import Image
    from src.jobs.worker import apply_text_overlays, _sanitize_overlay_text
    p = tmp_path / "bg.png"
    Image.new("RGB", (768, 1024), (30, 30, 40)).save(str(p))
    out = apply_text_overlays(str(p), [
        {"slot": "title", "text": "Tu cocina conectada"},
        {"slot": "contact", "text": "Escribinos 3624782201"},
        {"slot": "brand", "text": "Enlaces Chaco ñ áéí"},
        {"slot": "title", "text": "   "},
    ])
    assert out == str(p)
    assert Image.open(str(p)).size == (768, 1024)
    # emojis fuera (tofu), ñ/acentos adentro
    assert _sanitize_overlay_text("Hola 🍔👍") == "Hola"
    assert _sanitize_overlay_text("Enlaces Chaco ñ áéíóú") == "Enlaces Chaco ñ áéíóú"


def test_image_job_rejects_bad_overlay_slot():
    from fastapi.testclient import TestClient
    from src.gateway.app import app
    c = TestClient(app)
    r = c.post("/jobs/image", json={
        "prompt": "x", "quality": "balanced",
        "overlays": [{"slot": "marquesina", "text": "hola"}],
    })
    assert r.status_code == 400


def test_apply_chat_messages_renders_bubbles(tmp_path):
    from PIL import Image
    from src.jobs.worker import apply_chat_messages
    p = tmp_path / "bg.png"
    Image.new("RGB", (768, 1024), (30, 30, 40)).save(str(p))
    out = apply_chat_messages(str(p), [
        {"who": "cliente", "text": "Hola, ¿tienen promo 2x1?"},
        {"who": "bot", "text": "¡Hola! Sí, Burguer eco especial 2x1 a $10.000 🍔"},
        {"who": "bot", "text": "Tu pedido sale en 20 min"},
        {"who": "x", "text": "   "},
    ])
    assert out == str(p)
    # la imagen cambió (burbujas dibujadas, no fondo liso)
    assert Image.open(str(p)).tobytes() != Image.new("RGB", (768, 1024), (30, 30, 40)).tobytes()


def test_image_job_rejects_bad_message_who():
    from fastapi.testclient import TestClient
    from src.gateway.app import app
    c = TestClient(app)
    r = c.post("/jobs/image", json={
        "prompt": "x", "quality": "balanced",
        "messages": [{"who": "mozo", "text": "hola"}],
    })
    assert r.status_code == 400


def test_chat_autofit_no_overlap(tmp_path):
    from PIL import Image
    from src.jobs.worker import apply_chat_messages
    W, H = 768, 1024
    p = tmp_path / "bg.png"
    Image.new("RGB", (W, H), (30, 30, 40)).save(str(p))
    msgs = [{"who": "bot" if i % 2 else "cliente",
             "text": f"Mensaje de prueba número {i} con varias palabras para ocupar líneas"} for i in range(10)]
    apply_chat_messages(str(p), msgs)
    # verifica que no haya píxeles dibujados en la zona del contacto (>= 0.78H)
    px = Image.open(str(p)).load()
    bg = (30, 30, 40)
    bad = sum(1 for x in range(0, W, 8) for y in range(int(H * 0.78), H, 8) if px[x, y] != bg)
    assert bad == 0, f"chat invade zona contacto: {bad} px"


def test_image_job_with_photo_skips_gpu():
    """Foto base: completa sin switch ni ComfyUI (instantáneo)."""
    import base64
    from fastapi.testclient import TestClient
    from src.gateway.app import app
    from src.jobs.router import queue as job_queue
    try:
        job_queue.flush()
    except Exception:
        pass
    tiny = base64.b64encode(b"\x89PNG\r\nfakepng-bytes").decode()
    with patch("src.jobs.worker._get_orchestrator") as mock_get:
        mock_get.return_value = (MagicMock(), MagicMock())
        c = TestClient(app)
        r = c.post("/jobs/image", json={
            "prompt": "fondo", "quality": "balanced",
            "photo": f"data:image/png;base64,{tiny}",
            "overlays": [{"slot": "brand", "text": "Test"}],
        })
        assert r.status_code == 202
        job_id = r.json()["id"]
        import time
        for _ in range(20):
            body = c.get(f"/jobs/{job_id}").json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        body = c.get(f"/jobs/{job_id}").json()
        assert body["status"] == "completed", body
        assert body["result"]["file"].endswith(".png")
        # sin switch ni GPU: el orchestrator ni se consulta
        mock_get.assert_not_called()


def test_image_job_with_photo_restyle_goes_gpu(monkeypatch):
    """Foto + restyle: va por ComfyUI img2img (requiere switch)."""
    import base64
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from fastapi.testclient import TestClient
    from src.gateway.app import app
    from src.jobs.router import queue as job_queue
    try:
        job_queue.flush()
    except Exception:
        pass
    tiny = base64.b64encode(b"\x89PNG\r\nfakepng-bytes").decode()
    seen = {}

    async def fake_img2img(port, payload, job_id, photo):
        seen["called"] = True
        return {"file": "/tmp/x.png"}

    from unittest.mock import AsyncMock, MagicMock, patch
    with patch("src.jobs.worker._get_orchestrator") as mock_get, \
         patch("src.jobs.worker._run_comfyui_img2img", side_effect=fake_img2img) as mock_rs, \
         patch("src.jobs.worker._run_comfyui_t2i") as mock_t2i:
        mock_orch = MagicMock()
        mock_orch.switch_to = AsyncMock(return_value=True)
        mock_reg = MagicMock()
        mock_reg.resolve.return_value = {"service": "sdxl.service", "port": 8188}
        mock_get.return_value = (mock_orch, mock_reg)
        c = TestClient(app)
        r = c.post("/jobs/image", json={
            "prompt": "fondo", "quality": "balanced",
            "photo": f"data:image/png;base64,{tiny}", "restyle": True,
        })
        assert r.status_code == 202
        job_id = r.json()["id"]
        import time
        for _ in range(20):
            body = c.get(f"/jobs/{job_id}").json()
            if body["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert seen.get("called") is True
        mock_t2i.assert_not_called()
        mock_rs.assert_called_once()

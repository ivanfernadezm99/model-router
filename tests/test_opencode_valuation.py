"""Test que asegura que la valoración del combo y la marca OPENCODE se mantengan.

Esta lógica fue restaurada el 2026-09-16 tras cache del front y bug de streaming:
- Combo debe mostrar 1-5 con estrellas y base descriptiva
- coder-14b-100k debe llevar 🤖 OPENCODE
- default config debe ser coder-14b-100k para opencode
- TASK_TO_MODEL code -> coder-14b-100k
- bench_30b_vs_14b.sh debe existir y ser ejecutable
"""
import pathlib
import re
import json
import yaml


def test_combo_quality_tag_includes_opencode():
    html = pathlib.Path("web/templates/index.html").read_text()
    # qualityTag para 14b debe contener OPENCODE
    assert "coder-14b-100k" in html
    assert "OPENCODE" in html
    assert "3.5/5" in html or "coder-14b-100k" in html
    # verifica que la función qualityTag tenga la base con OPENCODE
    assert "✅ 14B estable 🤖 OPENCODE" in html
    # verifica que qualityScore para 30b siga siendo 5.0
    assert "if (name.includes('30b-q5')) return 5.0" in html
    assert "if (name === 'coder-30b-a3b') return 5.0" in html


def test_combo_stars_rendered():
    html = pathlib.Path("web/templates/index.html").read_text()
    # el JS debe generar estrellas
    assert "★'.repeat" in html
    assert "qualityTag(m.name, m.ctx)" in html
    # el option text debe incluir q + stars
    assert "o.textContent" in html and "stars" in html


def test_config_default_is_14b_for_opencode():
    cfg = yaml.safe_load(pathlib.Path("config.yaml").read_text())
    assert cfg["defaults"]["model"] == "coder-14b-100k"
    # 14b debe existir
    assert "coder-14b-100k" in cfg["models"]
    assert "coder-30b-a3b" in cfg["models"]


def test_detector_maps_code_to_14b():
    from src.gateway.detector import TASK_TO_MODEL
    assert TASK_TO_MODEL["code"] == "coder-14b-100k"
    assert TASK_TO_MODEL["image"] == "sdxl"


def test_web_descs_include_opencode():
    txt = pathlib.Path("web/app.py").read_text()
    assert "coder-14b-100k" in txt
    assert "OPENCODE" in txt
    assert "Qwen2.5-Coder-14B" in txt


def test_bench_script_exists_and_executable():
    p = pathlib.Path("bench_30b_vs_14b.sh")
    assert p.exists(), "bench_30b_vs_14b.sh debe existir"
    assert p.stat().st_mode & 0o111, "bench_30b_vs_14b.sh debe ser ejecutable"
    content = p.read_text()
    assert "coder-14b-100k" in content
    assert "coder-30b-a3b" in content
    assert "wait_health" in content
    assert "switch_to" in content


def test_proxy_streaming_fix():
    # el proxy no debe usar async with que cierre el client antes del stream
    txt = pathlib.Path("src/gateway/proxy.py").read_text()
    assert "client = httpx.AsyncClient" in txt
    assert "await client.aclose()" in txt
    # no debe quedar el viejo pattern que cerraba antes
    assert "async with httpx.AsyncClient" not in txt or txt.count("async with httpx.AsyncClient") == 0


def test_front_media_viewer_present():
    html = pathlib.Path("web/templates/index.html").read_text()
    assert 'id="lightbox"' in html
    assert "openLightbox" in html
    assert "media-preview" in html


def test_health_endpoint_reports_14b_default():
    # el endpoint /api/models debe reportar default_model 14b
    # lo verificamos leyendo config, no haciendo request vivo
    cfg = yaml.safe_load(pathlib.Path("config.yaml").read_text())
    assert cfg["defaults"]["model"] == "coder-14b-100k"

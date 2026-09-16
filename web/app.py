"""Flask web interface - Model Router combo + monitor (mismo origen)."""
import base64
import os

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
ROUTER_BASE = os.environ.get("ROUTER_BASE", "http://127.0.0.1:8000")
# endpoint del router por modelo del combo
MODEL_ENDPOINTS = {
    "video": "video",
    "i2v": "i2v",
    "avatar": "avatar",
    "image": "image",
}


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/monitor")
def monitor():
    return render_template("simple.html")


@app.route("/api/health")
def health():
    try:
        resp = requests.get(f"{ROUTER_BASE}/health", timeout=5)
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e), "model": None, "vram_used_mb": 0, "status": "router_no_responde"}), 502


@app.route("/api/queue")
def queue():
    try:
        resp = requests.get(f"{ROUTER_BASE}/jobs", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"jobs": [], "message": "Para crear jobs usá el combo o ./video.sh, ./i2v.sh, ./avatar.sh, ./mejorar.sh"}), 200
    except Exception as e:
        return jsonify({"jobs": [], "message": f"router no responde: {e}"}), 502


MODEL_DESCS = {
    "coder-30b-a3b": "⭐ RECOMENDADO — Qwen3-Coder-30B-A3B MoE (Q4). El mejor coder local 2026: 69.6% SWE-bench Verified, solo 3B activos/token → rápido y calidad superior al 14B. 100K híbrido: 18GB en VRAM + KV en RAM (--no-kv-offload), n_ctx 100096 verificado. Va por gateway /v1.",
    "coder-30b-150k": "🔥 Qwen3-Coder-30B-A3B 150K — mismo MoE 30B pero ventana extendida 150K para trabajos largos. YaRN 4.6x, ~20GB VRAM. Más contexto, leve pérdida vs 100K. Va por gateway /v1.",
    "coder-30b-190k": "⚡ Qwen3-Coder-30B-A3B 190K — tope VRAM 30B, ~22GB, YaRN 5.9x. Para trabajos muy largos, deja 2GB libres. Calidad con algo de pérdida pero usable. Va por gateway /v1.",
    "coder-30b-q5-100k": "💎 Qwen3-Coder-30B-A3B Q5 100K — mismo 30B pero Q5_K_M (21GB) mejor calidad que Q4. Si no entra en VRAM, KV va a RAM. Más preciso, deja ~1GB libre. Va por gateway /v1.",
    "coder-14b-100k": "Código y chat con Qwen2.5-Coder-14B (Q4). Estable 100K todo en GPU. Backup verificado. Va por gateway /v1.",
    "coder-14b-150k": "Intermedia: mismo 14B, 150K todo en GPU. Rápida, sin usar RAM. Va por gateway /v1.",
    "coder-14b-190k": "Tope GPU del 14B: 190K todo en VRAM (~21GB). Al límite, margen ~3GB. Va por gateway /v1.",
    "coder-14b-250k-ram": "250K con KV en RAM (--no-kv-offload). GPU ~11GB, KV ~14GB en RAM. Solo para contextos ultra-largos, lento por PCIe y YaRN 7.6x. Va por gateway /v1.",
    "coder-q5-65k": "Código alterno Q5 65K. Menos VRAM, contexto corto. Va por gateway /v1.",
    "wan-14b": "Texto → video desde cero. Solo prompt, sin archivo.",
    "wan-i2v-14b": "Foto o video → video animado (paneo/zoom cinematográfico). Lleva imagen + prompt.",
    "echomimic-v2": "Foto + audio → vos hablando (cara/torso sincronizado). Lleva imagen + audio + texto.",
    "sdxl": "Crear o mejorar fotos. Solo prompt para crear; foto + qué mejorar para editar.",
}


def _ctx_from_args(args):
    try:
        args = list(args or [])
        for i, a in enumerate(args):
            if a in ("-c", "--ctx-size") and i + 1 < len(args):
                return int(args[i + 1])
    except Exception:
        pass
    return None


@app.route("/api/models")
def models():
    try:
        resp = requests.get(f"{ROUTER_BASE}/jobs/models/list", timeout=10)
        if resp.status_code != 200:
            return jsonify({"active": None, "models": [], "error": resp.text[:200]}), resp.status_code
        data = resp.json()
        # args/contexto desde config local (el /models/list no trae args)
        cfg_args = {}
        try:
            import pathlib

            import yaml

            for cand in ("config.yaml", "/home/servidor/Descargas/model-router/config.yaml"):
                p = pathlib.Path(cand)
                if p.exists():
                    cfg = yaml.safe_load(p.read_text()) or {}
                    for name, spec in (cfg.get("models") or {}).items():
                        cfg_args[name] = spec.get("args") or []
                    break
        except Exception:
            pass
        default_model = None
        try:
            import pathlib
            import yaml as _yaml2
            for cand in ("config.yaml", "/home/servidor/Descargas/model-router/config.yaml"):
                p2 = pathlib.Path(cand)
                if p2.exists():
                    _cfg2 = _yaml2.safe_load(p2.read_text()) or {}
                    default_model = (_cfg2.get("defaults") or {}).get("model")
                    break
        except Exception:
            pass
        for m in data.get("models", []):
            m["desc"] = MODEL_DESCS.get(m["name"], "")
            m["ctx"] = _ctx_from_args(cfg_args.get(m["name"]))
        data["default_model"] = default_model
        return jsonify(data)
    except Exception as e:
        return jsonify({"active": None, "models": [], "error": str(e)}), 502


@app.route("/api/switch", methods=["POST"])
def switch():
    model = ((request.get_json(silent=True) or {}).get("model") or request.form.get("model") or "").strip()
    if not model:
        return jsonify({"error": "falta model"}), 400
    try:
        resp = requests.post(f"{ROUTER_BASE}/jobs/switch", json={"model": model}, timeout=15)
        if resp.status_code in (200, 202):
            return jsonify(resp.json()), 202
        return jsonify({"error": resp.text[:300]}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/switch-status")
def switch_status():
    try:
        resp = requests.get(f"{ROUTER_BASE}/jobs/switch/status", timeout=10)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"model": None, "status": "unknown", "detail": resp.text[:200]}), resp.status_code
    except Exception as e:
        return jsonify({"model": None, "status": "unknown", "detail": str(e)}), 502


@app.route("/api/logs")
def logs():
    import glob
    import subprocess

    out = {"router_log": [], "services": {}, "switch": {}, "jobs_failed": []}
    try:
        cands = sorted(glob.glob("/tmp/router_*.log"), key=os.path.getmtime)
        if cands:
            with open(cands[-1], errors="replace") as f:
                out["router_log"] = f.readlines()[-40:]
                out["router_log_file"] = cands[-1]
    except Exception as e:
        out["router_log"] = [f"no se pudo leer log: {e}"]
    for unit in ("llama-code-200k-ram.service", "llama-code-150k.service", "llama-code-q4.service"):
        try:
            p = subprocess.run(
                ["journalctl", "--user", "-u", unit, "-n", 15, "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            out["services"][unit] = (p.stdout or p.stderr or "(sin entradas)").strip().splitlines()[-15:]
        except Exception as e:
            out["services"][unit] = [f"journal no disponible: {e}"]
    try:
        r = requests.get(f"{ROUTER_BASE}/jobs/switch/status", timeout=10)
        if r.status_code == 200:
            out["switch"] = r.json()
    except Exception as e:
        out["switch"] = {"status": "unknown", "detail": str(e)}
    try:
        r = requests.get(f"{ROUTER_BASE}/jobs", timeout=10)
        if r.status_code == 200:
            for j in (r.json().get("jobs") or []):
                if j.get("status") == "failed":
                    out["jobs_failed"].append({"id": (j.get("id") or "?")[:8], "error": j.get("error")})
    except Exception:
        pass
    return jsonify(out)


@app.route("/api/job/<job_id>")
def job_status(job_id):
    try:
        resp = requests.get(f"{ROUTER_BASE}/jobs/{job_id}", timeout=5)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify({"id": job_id, "status": "unknown", "error": resp.text[:200]}), resp.status_code
    except Exception as e:
        return jsonify({"id": job_id, "status": "unknown", "error": str(e)}), 502


@app.route("/api/jobs/<job_id>/result")
def job_result(job_id):
    try:
        resp = requests.get(f"{ROUTER_BASE}/jobs/{job_id}/result", timeout=30)
        if resp.status_code == 200:
            return resp.content, 200, {"Content-Type": resp.headers.get("content-type", "application/octet-stream")}
        return jsonify({"error": resp.text[:200]}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


def _as_data_url(upload, default_mime="image/png"):
    if upload is None or not getattr(upload, "filename", ""):
        return None
    raw = upload.read()
    if not raw:
        return None
    mime = getattr(upload, "mimetype", "") or default_mime
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


@app.route("/api/submit", methods=["POST"])
def submit():
    model = (request.form.get("model") or "video").strip().lower()
    prompt = (request.form.get("prompt") or "").strip()
    quality = (request.form.get("quality") or "balanced").strip().lower()
    if model not in MODEL_ENDPOINTS:
        return jsonify({"error": f"modelo desconocido: {model}"}), 400
    if not prompt:
        return jsonify({"error": "falta prompt"}), 400

    endpoint = MODEL_ENDPOINTS[model]
    payload = {"prompt": prompt}
    try:
        if model == "image":
            if quality not in ("draft", "balanced", "max"):
                quality = "balanced"
            payload["quality"] = quality
        if model in ("i2v", "avatar"):
            img = _as_data_url(request.files.get("image"))
            if img:
                payload["image"] = img
            elif model == "avatar" and not request.files.get("audio"):
                pass
        if model == "avatar":
            aud = _as_data_url(request.files.get("audio"), default_mime="audio/wav")
            if aud:
                payload["audio"] = aud
        resp = requests.post(f"{ROUTER_BASE}/jobs/{endpoint}", json=payload, timeout=30)
        if resp.status_code in (200, 202):
            data = resp.json()
            return jsonify({"job_id": data.get("id"), "target": model, "status": "queued"}), 202
        return jsonify({"error": resp.text[:500]}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

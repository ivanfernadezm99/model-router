"""Flask web interface - Model Router combo + monitor (mismo origen)."""
import base64
import os

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
ROUTER_BASE = os.environ.get("ROUTER_BASE", "http://127.0.0.1:8000")
# endpoint del router por modelo del combo
MODEL_ENDPOINTS = {
     "vision": "vision",
     "video": "video",
     "i2v": "i2v",
     "avatar": "avatar",
     "image": "image",
   }


@app.route("/favicon.ico")
def favicon():
    import pathlib
    p = pathlib.Path(__file__).parent / "static" / "favicon.ico"
    if p.exists():
        from flask import send_file
        return send_file(str(p), mimetype="image/x-icon", max_age=86400)
    return ("", 204)

@app.route("/favicon.svg")
def favicon_svg():
    import pathlib
    p = pathlib.Path(__file__).parent / "static" / "favicon.svg"
    if p.exists():
        from flask import send_file
        return send_file(str(p), mimetype="image/svg+xml", max_age=86400)
    return ("", 204)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/monitor")
def monitor():
    return render_template("simple.html")


@app.route("/downloads")
def downloads():
    return render_template("downloads.html")


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
     "qwen3-27b-flagship": "🆕 NUEVO MODELO INSIGNIA — Qwen3.8-27B-UD-Q4_K_S.gguf | DENSO 27B Q4_K_S | 160K + reasoning. Mejor razonamiento general que los MoE 30B y que el 14B. ~15GB pesos, ~18GB VRAM. Candidato a nuevo default. Va por gateway /v1.",
     "vision-7b": "🔮 Visión — Qwen2.5-VL-7B-Instruct-q4_k_m.gguf | DENSO 7B + mmproj (1.3GB). Describe imágenes, screenshots, diagrams en markdown. :8083 CPU/RAM. 128K ctx. Adjuntá una imagen y pedí la descripción.",
     "coder-30b-a3b": "🥈 PUESTO 2 OPENCODE — Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf | MoE 30B (3B activos) Q4 | 100K todo en GPU. 12GB VRAM, YaRN bajo (3.1x) → más preciso que 190K, opción estable si 190K alucina. Va por gateway /v1.",
     "coder-30b-150k": "🔥 Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf | MoE 30B Q4 | 150K. Ventana extendida para trabajos largos. YaRN 4.6x, ~18GB VRAM. Intermedio entre 100K y 190K. Va por gateway /v1.",
     "coder-30b-190k": "🥇 PUESTO 1 OPENCODE MAX-CONTEXTO — Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf | MoE 30B Q4 | 190K todo en GPU — USA TODA LA VRAM (22GB/24GB). Determinístico temp 0.2 + DRY 0.9 + top_p 0.85 para no alucinar con YaRN 5.9x. Va por gateway /v1.",
     "coder-30b-q5-100k": "💎 Qwen3-Coder-30B-A3B-Instruct-Q5_K_M.gguf | MoE 30B Q5 | 100K. Mejor calidad que Q4 (21GB); si no entra, KV a RAM. Deja ~1GB libre. Va por gateway /v1.",
      "coder-14b-100k": "🥉 PUESTO 3 OPENCODE — Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf | DENSO 14B Q4 | 100K todo en GPU. Ex-puesto 1 hasta 2026-09-18: rápido, 14GB VRAM, tool-calling Jinja pero frágil MCP (pide disculpas) y loops corregidos. Relegado. Va por gateway /v1.",
      "qwen3-14b-190k": "🆕 Qwen3-14B-Instruct-Q4_K_M.gguf | DENSO 14B Q4 | 190K, 8 chats simultáneos. Generalista: chatbots, asistentes, redacción, razonamiento y code liviano. ~9GB pesos, ~19GB VRAM. Para code pesado preferí los coder-30b MoE. Va por gateway /v1.",
     "coder-14b-150k": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf | DENSO 14B Q4 | 150K todo en GPU. Rápida, sin usar RAM. Va por gateway /v1.",
     "coder-14b-190k": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf | DENSO 14B Q4 | 190K todo en VRAM (~21GB). Al límite, margen ~3GB. Va por gateway /v1.",
     "coder-14b-250k-ram": "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf | DENSO 14B Q4 | 250K con KV en RAM (--no-kv-offload). GPU ~11GB, KV ~14GB en RAM. Solo contextos ultra-largos, lento por PCIe y YaRN 7.6x. Va por gateway /v1.",
     "coder-q5-65k": "Código alterno Q5 65K. Menos VRAM, contexto corto. Va por gateway /v1.",
     "wan-14b": "Wan2.1-T2V-14B | difusión 14B | Texto → video desde cero. Solo prompt, sin archivo.",
     "wan-i2v-14b": "Wan2.1-I2V-14B-720P | difusión 14B | Foto o video → video animado (paneo/zoom cinematográfico). Lleva imagen + prompt.",
     "echomimic-v2": "EchoMimicV2 | difusión | Foto + audio → vos hablando (cara/torso sincronizado). Lleva imagen + audio + texto.",
     "sdxl": "SDXL | difusión | Crear o mejorar fotos. Solo prompt para crear; foto + qué mejorar para editar.",
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


@app.route("/job/<job_id>")
def view_job(job_id):
    """Vista frontend para un job específico — renderiza detalle + preview si hay file."""
    return render_template("job.html", job_id=job_id)

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
        # stream to support video seeking without loading whole file in memory
        # forward Range header so <video> seeking works (206 Partial Content)
        fwd_headers = {}
        rng = request.headers.get("Range")
        if rng:
            fwd_headers["Range"] = rng
        resp = requests.get(f"{ROUTER_BASE}/jobs/{job_id}/result", timeout=30, stream=True, headers=fwd_headers)
        if resp.status_code in (200, 206):
            from flask import Response

            headers = {}
            ct = resp.headers.get("content-type", "application/octet-stream")
            headers["Content-Type"] = ct
            # let browser display inline (lightbox <img>/<video>), not force download
            # download is triggered via <a download> on the frontend
            if resp.headers.get("content-disposition"):
                headers["Content-Disposition"] = resp.headers.get("content-disposition").replace("attachment", "inline")
            else:
                # fallback: inline without filename, frontend supplies download attr
                headers["Content-Disposition"] = "inline"
            for hk in ("content-length", "content-range", "accept-ranges", "etag", "last-modified"):
                if resp.headers.get(hk):
                    headers[hk] = resp.headers.get(hk)
            headers["Cache-Control"] = "public, max-age=3600"
            if "accept-ranges" not in (k.lower() for k in headers):
                headers["Accept-Ranges"] = "bytes"

            def generate():
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk

            return Response(generate(), status=resp.status_code, headers=headers)
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
            else:
                return jsonify({"error": f"el modelo {model} requiere una imagen (adjuntá foto/video base)"}), 400
        if model == "avatar":
            aud = _as_data_url(request.files.get("audio"), default_mime="audio/wav")
            if aud:
                payload["audio"] = aud
            else:
                return jsonify({"error": "el modelo avatar requiere un audio (adjuntá tu voz)"}), 400

        # Visión: va por /v1/chat/completions con formato multimodal OpenAI
        if model == "vision":
            img = request.files.get("image")
            if not img:
                return jsonify({"error": "el modelo visión requiere una imagen adjunta"}), 400
            img_data = _as_data_url(img)
            messages = [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": img_data}}
                ]}
            ]
            resp = requests.post(
                f"{ROUTER_BASE}/v1/chat/completions",
                json={"messages": messages, "stream": False, "temperature": 0.3},
                headers={"X-Model-Hint": "vision", "Content-Type": "application/json"},
                timeout=120,
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return jsonify({"job_id": "vision-" + data.get("id", "local"), "target": model, "status": "completed", "result": {"content": content}}), 200
            return jsonify({"error": f"vision error: {resp.status_code} {resp.text[:200]}"}), resp.status_code

        resp = requests.post(f"{ROUTER_BASE}/jobs/{endpoint}", json=payload, timeout=30)
        if resp.status_code in (200, 202):
            data = resp.json()
            return jsonify({"id": data.get("id"), "target": model, "status": "queued"}), 202
        return jsonify({"error": resp.text[:500]}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

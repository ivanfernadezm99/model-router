"""Flask web interface - Model Router combo + monitor (mismo origen)."""
import base64
import logging
import os

import requests
from flask import Flask, jsonify, render_template, request

log = logging.getLogger("model-router-web")

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


GENERACION_SUBDIR = ("static", "generacion")
# De dónde salen las piezas de avisos (para el cartel de "faltan archivos").
AVISOS_SRC = ("/media/servidor/d0a196c3-2d36-4431-b15d-8ea078ad8222"
              "/proyectos/contenido-visual-ia/output/enlaces-chaco/pro")
AVISO_TITLES = {
    "aviso-00-hoja-de-contacto.png": "Hoja de contacto · vista de conjunto",
    "aviso-01-arca.png": "01 · Facturación ARCA",
    "aviso-02-offline.png": "02 · Modo offline",
    "aviso-03-sucursales.png": "03 · Multi-sucursal",
    "aviso-04-stock.png": "04 · Control de stock",
    "aviso-05-ventas.png": "05 · Reportes",
    "aviso-06-fiado.png": "06 · Cuenta corriente",
    "aviso-07-cobro.png": "07 · Cobro rápido",
    "aviso-08-soporte.png": "08 · Soporte local",
    "aviso-09-planes.png": "09 · Planes",
    "aviso-10-arranque.png": "10 · Arranque rápido",
    "variante_A.png": "Variante A · demo-imagen",
    "variante_B.png": "Variante B · demo-imagen",
    "enlaces-chaco.png": "Aviso Enlaces Chaco (MVP)",
    "ad-enlaces-chaco-20s-16x9.mp4": "Ad 20s · 16:9 (web / YouTube)",
    "ad-enlaces-chaco-20s-1x1.mp4": "Ad 20s · 1:1 (feed)",
    "ad-enlaces-chaco-20s-9x16.mp4": "Ad 20s · 9:16 (Reels / Stories)",
}


def _generacion_dir():
    import pathlib

    return pathlib.Path(__file__).parent.joinpath(*GENERACION_SUBDIR)


def _asset_label(name):
    if name in AVISO_TITLES:
        return AVISO_TITLES[name]
    stem = name.rsplit(".", 1)[0]
    return stem.replace("_", " ").replace("-", " ").strip().capitalize()


_MP4_META_CACHE = {}


def _mp4_note(p):
    """Describe an MP4 from its real metadata (cached by mtime + size)."""
    import json
    import subprocess

    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    cached = _MP4_META_CACHE.get(str(p))
    if cached and cached[0] == stamp:
        return cached[1]
    note = None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            info = json.loads(proc.stdout)
            streams = info.get("streams", [])
            v = next((s for s in streams if s.get("codec_type") == "video"), None)
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            dur = float(info.get("format", {}).get("duration") or 0)
            if v and v.get("width"):
                note = (f"MP4 {v['width']}×{v['height']} · {dur:.1f} s"
                        f" · {'con audio' if has_audio else 'sin audio'}")
    except Exception:
        note = None
    _MP4_META_CACHE[str(p)] = (stamp, note)
    return note


def _asset_note(name, path=None):
    if name.startswith("aviso-00"):
        return "Montaje 5×2 de las 10 piezas · clic para ampliarla"
    if name.startswith("aviso-"):
        return "Aviso 1024×1024 · fondo IA + composición PIL"
    if name.lower().endswith(".mp4"):
        p = path if path is not None else (_generacion_dir() / name)
        return _mp4_note(p) or "Video MP4"
    return "Aviso publicitario (PNG)"


@app.route("/generacion")
def generacion():
    """Galería de artefactos de contenido visual IA.

    La carpeta `web/static/generacion/` es la fuente de verdad: cualquier PNG o
    MP4 que esté ahí aparece solo, sin tocar código. Si la carpeta no existe se
    crea y la página se ve vacía, nunca un 500."""
    base = _generacion_dir()
    if not base.is_dir():
        base.mkdir(parents=True, exist_ok=True)

    names = [p.name for p in base.iterdir()
             if p.is_file() and p.suffix.lower() in (".png", ".mp4")
             and not p.name.startswith(".")]
    # avisos primero (y la hoja de contacto al frente), después el resto
    names.sort(key=lambda n: (0 if n.startswith("aviso-00") else
                              1 if n.startswith("aviso-") else 2, n))

    sheet, assets = None, []
    for name in names:
        p = base / name
        entry = {
            "name": name,
            "url": f"/static/generacion/{name}",
            "exists": True,
            "size_mb": round(p.stat().st_size / 1048576, 2),
            "label": _asset_label(name),
            "note": _asset_note(name, p),
            "is_video": p.suffix.lower() == ".mp4",
        }
        if name.startswith("aviso-00"):
            sheet = entry
        else:
            assets.append(entry)

    # La carpeta es la fuente de verdad: NO hay una lista de "archivos que deben
    # estar". Si borrás una pieza desaparece y la página no la reclama. Solo
    # mostramos un aviso neutro cuando la galería quedó vacía.
    return render_template("generacion.html", assets=assets, sheet=sheet,
                           empty=not names, count=len(names),
                           avisos_src=AVISOS_SRC, base_dir=str(base))


@app.route("/generacion/delete/<name>", methods=["POST"])
def generacion_delete(name):
    """Borra un artefacto de la galería.

    El nombre se sanea: tiene que ser un nombre simple (sin separadores de ruta)
    y la ruta resuelta tiene que quedar dentro de la carpeta de la galería."""
    import pathlib
    from flask import redirect, url_for

    base = _generacion_dir().resolve()
    simple = bool(name) and not name.startswith(".") and pathlib.PurePath(name).name == name
    if simple:
        p = (base / name).resolve()
        if p.parent == base and p.is_file() and p.suffix.lower() in (".png", ".mp4"):
            p.unlink()
    return redirect(url_for("generacion"))


@app.route("/generacion/upload-to-composer", methods=["POST"])
def generacion_upload_to_composer():
    """Proxy same-origin: sube la pieza al backend POS (presign Zernio) y
    devuelve {publicUrl}. Evita CORS: el navegador solo habla con :5000.
    El JWT viaja en el header Authorization del request y no se persiste."""
    backend = os.environ.get(
        "COMPOSER_BACKEND_API",
        "https://posbackend-staging-prod.up.railway.app/api",
    )
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "falta file"}), 400
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "falta JWT (Authorization: Bearer)"}), 401
    try:
        resp = requests.post(
            f"{backend}/Zernio/media/upload",
            headers={"Authorization": auth},
            files={"file": (f.filename, f.read(), f.mimetype or "application/octet-stream")},
            timeout=300,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"backend inaccesible: {e}"}), 502
    try:
        body = resp.json()
    except ValueError:
        return jsonify({"error": "respuesta ilegible del backend"}), 502
    if resp.status_code == 200 and body.get("isSuccess"):
        public_url = (body.get("data") or {}).get("publicUrl")
        if not public_url:
            return jsonify({"error": "el backend no devolvió publicUrl"}), 502
        return jsonify({"publicUrl": public_url})
    message = body.get("message") or f"upload falló ({resp.status_code})"
    return jsonify({"error": message}), resp.status_code


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
     "qwen3-27b-xl": "🆕 Qwen3.8-27B-UD-Q4_K_XL.gguf | DENSO 27B Q4_K_XL | 160K sin reasoning explícito. Hermano del flagship con quant XL (~17GB): un poco más de calidad, un poco más de VRAM (~20GB). Para comparar contra el Q4_K_S. Va por gateway /v1.",
     "vision-7b": "🔮 Visión — Qwen2.5-VL-7B-Instruct-q4_k_m.gguf | DENSO 7B + mmproj (1.3GB). Describe imágenes, screenshots, diagrams en markdown. :8083 CPU/RAM. 128K ctx. Adjuntá una imagen y pedí la descripción.",
     "coder-30b-a3b": "🥈 PUESTO 2 OPENCODE — Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf | MoE 30B (3B activos) Q4 | 100K todo en GPU. 12GB VRAM, YaRN bajo (3.1x) → más preciso que 190K, opción estable si 190K alucina. Va por gateway /v1.",
     "coder-30b-128k": "🔷 Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf | MoE 30B Q4 | 128K todo en GPU. Punto medio: más margen que 100K sin empujar a RAM como 150K/190K. ~15GB VRAM. Recomendado para OpenCode. Va por gateway /v1.",
     "coder-30b-150k": "🔥 Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf | MoE 30B Q4 | 150K todo en GPU (sin offload de entrada; si falla por VRAM, agregar --no-kv-offload). ~18GB VRAM. Va por gateway /v1.",
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
     "sdxl": "SDXL | difusión | Legacy (reemplazado por qwen-image para fotos).",
     "qwen-image": "🆕 NUEVO Qwen-Image 20B Q4 | FOTO FIJA publicitaria con texto (avisos, promos, carteles). Genera 1 imagen (~13 min). Distinto de video/i2v/avatar que generan VIDEO con movimiento.",
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
        resp = requests.post(f"{ROUTER_BASE}/jobs/switch", json={"model": model}, timeout=30)
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


@app.route("/api/job/<job_id>", methods=["DELETE"])
def job_cancel(job_id):
    log.warning(
        "AUDIT front-cancel job=%s remote=%s ua=%s",
        job_id, request.remote_addr, (request.headers.get("User-Agent", "-"))[:120],
    )
    try:
        resp = requests.delete(f"{ROUTER_BASE}/jobs/{job_id}", timeout=10)
        try:
            return jsonify(resp.json()), resp.status_code
        except Exception:
            return jsonify({"id": job_id, "error": resp.text[:200]}), resp.status_code
    except Exception as e:
        return jsonify({"id": job_id, "error": str(e)}), 502


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


MAX_PATH_FILE_MB = 200


def _data_url_from_path(path_str, default_mime="image/png"):
    """Lee un archivo del disco del servidor (el front corre local).
    Alternativa cuando el file picker del SO no entrega el archivo."""
    import mimetypes
    p = (path_str or "").strip()
    if not p:
        return None
    if not os.path.isabs(p):
        return None, "la ruta tiene que ser absoluta (ej: /home/servidor/Imágenes/foto.png)"
    if not os.path.isfile(p):
        return None, f"no existe o no es archivo: {p[:120]}"
    size = os.path.getsize(p)
    if size > MAX_PATH_FILE_MB * 1024 * 1024:
        return None, f"archivo muy grande ({size // 1048576}MB, tope {MAX_PATH_FILE_MB}MB)"
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except Exception as e:
        return None, f"no se pudo leer: {e}"
    if not raw:
        return None, "archivo vacío"
    mime = mimetypes.guess_type(p)[0] or default_mime
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"), None


def _data_url_or_path(upload, path_str, default_mime="image/png"):
    """Upload gana; si no hay, ruta de disco. Retorna (data_url, error)."""
    if upload is not None and getattr(upload, "filename", ""):
        return _as_data_url(upload, default_mime), None
    if (path_str or "").strip():
        return _data_url_from_path(path_str, default_mime)
    return None, None


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
    if model == "image":
        # foto base opcional: el worker la usa como fondo + overlays (sin IA).
        # A diferencia de i2v (que la anima a video), acá queda FIJA + textos.
        bg, bg_err = _data_url_or_path(request.files.get("image"), request.form.get("image_path"))
        if bg_err:
            return jsonify({"error": f"foto: {bg_err}"}), 400
        if bg:
            payload["photo"] = bg
    if model == "image":
        ov_raw = request.form.get("overlays") or ""
        if ov_raw.strip():
            try:
                import json as _json
                ovs = _json.loads(ov_raw)
                if isinstance(ovs, list) and ovs:
                    payload["overlays"] = [
                        {"slot": str(o.get("slot", "")), "text": str(o.get("text", ""))[:200]}
                        for o in ovs if isinstance(o, dict) and o.get("text")
                    ]
            except Exception:
                pass
        if (request.form.get("restyle") or "").strip().lower() in ("true", "1", "on"):
            payload["restyle"] = True
        msg_raw = request.form.get("messages") or ""
        if msg_raw.strip():
            try:
                import json as _json2
                ms = _json2.loads(msg_raw)
                if isinstance(ms, list) and ms:
                    payload["messages"] = [
                        {"who": str(x.get("who", "bot")), "text": str(x.get("text", ""))[:160]}
                        for x in ms if isinstance(x, dict) and x.get("text")
                    ][:12]
            except Exception:
                pass
    try:
        if model == "image":
            if quality not in ("draft", "balanced", "max"):
                quality = "balanced"
            payload["quality"] = quality
        if model in ("i2v", "avatar"):
            img, img_err = _data_url_or_path(request.files.get("image"), request.form.get("image_path"))
            if img_err:
                return jsonify({"error": f"imagen: {img_err}"}), 400
            if img:
                payload["image"] = img
            else:
                return jsonify({"error": f"el modelo {model} requiere una imagen (adjuntá foto/video base o pegá la ruta)"}), 400
        if model == "avatar":
            aud, aud_err = _data_url_or_path(request.files.get("audio"), request.form.get("audio_path"), default_mime="audio/wav")
            if aud_err:
                return jsonify({"error": f"audio: {aud_err}"}), 400
            if aud:
                payload["audio"] = aud
            else:
                return jsonify({"error": "el modelo avatar requiere un audio (adjuntá tu voz o pegá la ruta)"}), 400

        # Visión: va por /v1/chat/completions con formato multimodal OpenAI
        if model == "vision":
            img_data, img_err = _data_url_or_path(request.files.get("image"), request.form.get("image_path"))
            if img_err:
                return jsonify({"error": f"imagen: {img_err}"}), 400
            if not img_data:
                return jsonify({"error": "el modelo visión requiere una imagen adjunta o su ruta"}), 400
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
            # el gateway devuelve "id"; el frontend espera "job_id" (index.html).
            # Se devuelven ambas claves para no romper ningun consumidor.
            return jsonify({"id": data.get("id"), "job_id": data.get("id"), "target": model, "status": "queued"}), 202
        return jsonify({"error": resp.text[:500]}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

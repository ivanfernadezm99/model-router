"""Job worker — wires /jobs to Orchestrator (VRAM exclusive) + backend.

Image and video jobs share the same VRAM lock as /v1 gateway, so a burst
of 5 videos + 10 images via /jobs also serializes to 2 switches (wan-14b -> sdxl).
Jobs run as FastAPI BackgroundTasks; status is persisted in Redis (JobQueue).

/v1 is the sync gateway (LLM always via /v1, code default).
/jobs is the async batch API (poll GET /jobs/{id}) — now wired, not stub.
"""

import logging
import asyncio
import os
import shutil

import httpx

from src.jobs.queue import JobQueue

logger = logging.getLogger(__name__)

# disco con más espacio libre (375G) para outputs generados
OUTPUT_DIR = "/media/servidor/d0a196c3-2d36-4431-b15d-8ea078ad8222/model-router-output"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _move_file_to_output(job_id: str, payload: dict) -> None:
    """Si el backend devolvió un file en disco, lo copia al OUTPUT_DIR del disco grande."""
    candidates = []
    for k in ("file", "video_path", "image_path", "output", "path", "video", "image", "result"):
        v = payload.get(k)
        if isinstance(v, str) and v and os.path.exists(v) and os.path.isfile(v):
            candidates.append((k, v))
    # también busca file anidado en payload interno
    if not candidates:
        for k, v in list(payload.items()):
            if isinstance(v, dict):
                for kk in ("file", "path"):
                    vv = v.get(kk)
                    if isinstance(vv, str) and vv and os.path.exists(vv):
                        candidates.append((k, vv))
    if not candidates:
        return
    # toma el primero
    key, src = candidates[0]
    try:
        # si ya está en OUTPUT_DIR, solo normaliza a payload["file"]
        if os.path.commonpath([os.path.abspath(src), os.path.abspath(OUTPUT_DIR)]) == os.path.abspath(OUTPUT_DIR):
            payload["file"] = src
            return
        basename = os.path.basename(src)
        dst = os.path.join(OUTPUT_DIR, f"{job_id}_{basename}")
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
            logger.info(f"copied output {src} -> {dst} for job {job_id}")
        payload["file"] = dst
        if key != "file":
            payload[f"orig_{key}"] = src
    except Exception as e:
        logger.warning(f"failed to move {src} to output dir: {e}")
        payload["file"] = src


def _notify_telegram(job_id: str, kind: str, status: str, payload: dict, error: str | None = None):
    """Send Telegram notification if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        prompt = payload.get("prompt", "")[:200]
        if status == "completed":
            text = f"✅ {kind} job {job_id[:8]} terminado\nPrompt: {prompt}\nTarget: {payload.get('target', kind)}"
            if payload.get("file"):
                text += f"\nArchivo: {payload['file']}"
        else:
            text = f"❌ {kind} job {job_id[:8]} falló\nPrompt: {prompt}\nError: {error or 'unknown'}"
        # fire and forget, timeout 5s
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as exc:
        logger.warning(f"telegram notify failed for {job_id}: {exc}")


def _get_orchestrator():
    # lazy import to avoid circular import with gateway.app
    import sys
    # El gateway corre como `python3 -m src.gateway.app` (__main__), pero este
    # import crea OTRA instancia del módulo (doble import clásico): dos
    # orchestrators → /health decía coder y /jobs/models/list decía qwen-image
    # con el mismo proceso (2026-10-07). Se usa la instancia servida (__main__)
    # cuando existe; en tests (pytest como __main__) se usa la del módulo.
    main = sys.modules.get("__main__")
    if (
        main is not None
        and getattr(main, "orchestrator", None) is not None
        and getattr(main, "registry", None) is not None
    ):
        orchestrator, registry = main.orchestrator, main.registry
    else:
        from src.gateway.app import orchestrator, registry
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"_get_orchestrator: orchestrator id={id(orchestrator)} active_model={orchestrator.active_model}")
    return orchestrator, registry


def _target_for_kind(kind: str) -> str:
    mapping = {"video": "wan-14b", "i2v": "wan-i2v-14b", "avatar": "echomimic-v2", "image": "qwen-image"}
    return mapping.get(kind, "sdxl")


QWEN_IMAGE_NEGATIVE = "blurry, distorted, deformed text, watermark, low quality"

# Cuando el front manda overlays (texto exacto), el fondo sale SIN texto y las
# letras las pega PIL: 100% fieles, sin importar idioma o marca.
BG_NO_TEXT_SUFFIX = " Clean background only, absolutely no text, no letters, no numbers, no watermark, no logo, no emblem."


def _sanitize_overlay_text(text: str) -> str:
    """Saca emojis y símbolos sin glifo (DejaVu los dibuja como tofu)."""
    import unicodedata
    return "".join(c for c in (text or "") if not unicodedata.category(c).startswith("So")).strip()


def apply_text_overlays(image_path: str, overlays: list[dict]) -> str:
    """Pega textos exactos sobre la imagen. Layout fijo por slot.
    Retorna el mismo path (modificado in-place)."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        for p in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        return ImageFont.load_default()

    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    d = ImageDraw.Draw(img)
    items = [o for o in (overlays or []) if (o.get("text") or "").strip()]
    if not items:
        return image_path

    def center_text(y: int, text: str, size: int, fill=(255, 255, 255), stroke=(0, 0, 0)):
        f = font(size)
        bbox = d.textbbox((0, 0), text, font=f, stroke_width=2)
        tw = bbox[2] - bbox[0]
        # achica hasta que entre con margen
        while tw > W - 60 and size > 20:
            size -= 4
            f = font(size)
            bbox = d.textbbox((0, 0), text, font=f, stroke_width=2)
            tw = bbox[2] - bbox[0]
        d.text(((W - tw) / 2, y), text, font=f, fill=fill, stroke_width=2, stroke_fill=stroke)
        return y + (bbox[3] - bbox[1]) + 18

    y = int(H * 0.06)
    for o in items:
        slot, text = o.get("slot"), _sanitize_overlay_text(o.get("text", ""))
        if not text:
            continue
        if slot == "title":
            y = center_text(y, text.upper(), int(W * 0.075))
        elif slot == "subtitle":
            y = center_text(y, text, int(W * 0.048), fill=(255, 235, 150))
        elif slot == "contact":
            f = font(int(W * 0.05))
            bbox = d.textbbox((0, 0), text, font=f, stroke_width=2)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pad = 24
            d.rounded_rectangle(
                [(W - tw) / 2 - pad, H * 0.80, (W + tw) / 2 + pad, H * 0.80 + th + pad * 2],
                radius=28, fill=(20, 140, 60),
            )
            d.text(((W - tw) / 2, H * 0.80 + pad), text, font=f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
        elif slot == "brand":
            f = font(int(W * 0.042))
            bbox = d.textbbox((0, 0), text, font=f, stroke_width=2)
            tw = bbox[2] - bbox[0]
            d.text((W - tw - 30, H - (bbox[3] - bbox[1]) - 34), text, font=f, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
    img.save(image_path)
    logger.warning("overlays aplicados job-file=%s n=%s", image_path, len(items))
    return image_path


def apply_chat_messages(image_path: str, messages: list[dict]) -> str:
    """Burbujas estilo WhatsApp con mensajes reales (bot=verde claro derecha,
    cliente=blanco izquierda). Texto exacto, 100% legible."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        for p in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        return ImageFont.load_default()

    msgs = [
        {"who": m.get("who", "bot"), "text": _sanitize_overlay_text(m.get("text", ""))}
        for m in (messages or [])[:12]
    ]
    msgs = [m for m in msgs if m["text"]]
    if not msgs:
        return image_path
    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    d = ImageDraw.Draw(img)
    fsize = int(W * 0.032)
    max_w = int(W * 0.62)
    top_lim, bot_lim = H * 0.30, H * 0.72

    def layout(fs):
        f = font(fs)
        out = []
        for m in msgs:
            words, lines, cur = m["text"].split(), [], ""
            for w_ in words:
                t = (cur + " " + w_).strip()
                if d.textlength(t, font=f) <= int(W * 0.62):
                    cur = t
                else:
                    lines.append(cur)
                    cur = w_
            if cur:
                lines.append(cur)
            out.append((m["who"], lines[:4]))
        lh = int(fs * 1.45)
        return f, out, lh

    f, lines_all, line_h = layout(fsize)
    pad, gap = 18, 12
    # achica letra hasta que el bloque entre sin pisar contacto (0.80)
    while fsize > 16:
        total = sum(len(l) * line_h + pad * 2 + gap for _, l in lines_all) - gap
        if total <= (bot_lim - top_lim):
            break
        fsize -= 2
        f, lines_all, line_h = layout(fsize)
    # si ni así entra, conserva la cola (lo último es lo que vende)
    while len(lines_all) > 4:
        total = sum(len(l) * line_h + pad * 2 + gap for _, l in lines_all) - gap
        if total <= (bot_lim - top_lim):
            break
        lines_all.pop(0)
    y = int(top_lim)
    for who, lines in lines_all:
        bh = len(lines) * line_h + pad * 2
        # ancho = línea más larga
        bw = max(d.textlength(l, font=f) for l in lines)
        bw = min(bw + pad * 2, max_w + pad * 2)
        if who == "bot":
            x0, fill = W - bw - 40, (200, 245, 200)
        else:
            x0, fill = 40, (255, 255, 255)
        d.rounded_rectangle([x0, y, x0 + bw, y + bh], radius=22, fill=fill, outline=(180, 180, 180), width=2)
        ty = y + pad
        for l in lines:
            d.text((x0 + pad, ty), l, font=f, fill=(20, 20, 20))
            ty += line_h
        y += bh + gap
    img.save(image_path)
    logger.warning("chat aplicado job-file=%s n=%s", image_path, len(msgs))
    return image_path

# Qwen-Image base tarda ~13 min por foto en 3090 (40 steps 1024x1536).
COMFY_POLL_TIMEOUT_S = 1500
COMFY_POLL_INTERVAL_S = 10


def _qwen_image_workflow(prompt: str, width: int, height: int, steps: int, cfg: float, seed: int) -> dict:
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "qwen-image-Q4_K_M.gguf", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": QWEN_IMAGE_NEGATIVE}},
        "6": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "ModelSamplingFlux", "inputs": {"model": ["1", 0], "max_shift": 1.15, "base_shift": 0.5, "width": width, "height": height}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0], "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "router-image", "images": ["9", 0]}},
    }


async def _run_comfyui_img2img(port: int, payload: dict, job_id: str, photo_path: str) -> dict:
    """Restyle: redibuja la foto 'similar pero nueva' (denoise 0.65).
    Los textos finales los pega PIL después — lo que salga deformado no importa."""
    import random
    import time
    width = int(payload.get("width") or 768)
    height = int(payload.get("height") or 768)
    steps = int(payload.get("steps") or 30)
    cfg = float(payload.get("cfg") or 7.0)
    seed = int(payload.get("seed") or random.randint(1, 2**31 - 1))
    prompt = payload.get("prompt", "") + BG_NO_TEXT_SUFFIX
    with open(photo_path, "rb") as f:
        photo_bytes = f.read()
    fname = f"{job_id}-src.png"
    wf = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "qwen-image-Q4_K_M.gguf", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": QWEN_IMAGE_NEGATIVE}},
        "6": {"class_type": "LoadImage", "inputs": {"image": fname}},
        "7": {"class_type": "VAEEncode", "inputs": {"pixels": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "ModelSamplingFlux", "inputs": {"model": ["1", 0], "max_shift": 1.15, "base_shift": 0.5, "width": width, "height": height}},
        "9": {"class_type": "KSampler", "inputs": {"model": ["8", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["7", 0], "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple", "denoise": 0.65}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"filename_prefix": "router-restyle", "images": ["10", 0]}},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        up = await client.post(
            f"http://127.0.0.1:{port}/upload/image",
            files={"image": (fname, photo_bytes, "image/png")},
            data={"overwrite": "true"},
        )
        up.raise_for_status()
        r = await client.post(f"http://127.0.0.1:{port}/prompt", json={"prompt": wf})
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]
        logger.warning("comfyui restyle job=%s prompt_id=%s seed=%s", job_id, prompt_id, seed)
        deadline = time.monotonic() + COMFY_POLL_TIMEOUT_S
        while True:
            h = await client.get(f"http://127.0.0.1:{port}/history/{prompt_id}")
            h.raise_for_status()
            hist = h.json().get(prompt_id, {})
            outs = (hist.get("outputs") or {}).get("11", {}).get("images", [])
            if outs:
                break
            if time.monotonic() >= deadline:
                return {"backend_error": f"comfyui timeout {COMFY_POLL_TIMEOUT_S}s sin imagen (prompt {prompt_id})"}
            await asyncio.sleep(COMFY_POLL_INTERVAL_S)
        img = outs[0]
        v = await client.get(
            f"http://127.0.0.1:{port}/view",
            params={"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")},
        )
        v.raise_for_status()
        dst = os.path.join(OUTPUT_DIR, f"{job_id}.png")
        with open(dst, "wb") as f:
            f.write(v.content)
        overlays = payload.get("overlays") or []
        if overlays:
            try:
                apply_text_overlays(dst, overlays)
            except Exception as exc:
                logger.warning("overlays failed job=%s err=%s", job_id, exc)
        messages = payload.get("messages") or []
        if messages:
            try:
                apply_chat_messages(dst, messages)
            except Exception as exc:
                logger.warning("chat failed job=%s err=%s", job_id, exc)
        logger.warning("comfyui restyle job=%s done file=%s", job_id, dst)
        return {"file": dst, "seed": seed, "comfyui_file": img["filename"], "restyled": True}


async def _run_comfyui_t2i(port: int, payload: dict, job_id: str) -> dict:
    """Genera foto fija con Qwen-Image vía ComfyUI. Retorna dict con file o backend_error."""
    import random
    import time
    width = int(payload.get("width") or 768)
    height = int(payload.get("height") or 768)
    steps = int(payload.get("steps") or 30)
    cfg = float(payload.get("cfg") or 7.0)
    seed = int(payload.get("seed") or random.randint(1, 2**31 - 1))
    prompt = payload.get("prompt", "")
    overlays = payload.get("overlays") or []
    if overlays:
        prompt = prompt + BG_NO_TEXT_SUFFIX
    wf = _qwen_image_workflow(prompt, width, height, steps, cfg, seed)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"http://127.0.0.1:{port}/prompt", json={"prompt": wf})
        r.raise_for_status()
        prompt_id = r.json()["prompt_id"]
        logger.warning("comfyui t2i job=%s prompt_id=%s seed=%s", job_id, prompt_id, seed)
        deadline = time.monotonic() + COMFY_POLL_TIMEOUT_S
        while True:
            h = await client.get(f"http://127.0.0.1:{port}/history/{prompt_id}")
            h.raise_for_status()
            hist = h.json().get(prompt_id, {})
            outs = (hist.get("outputs") or {}).get("10", {}).get("images", [])
            if outs:
                break
            if time.monotonic() >= deadline:
                return {"backend_error": f"comfyui timeout {COMFY_POLL_TIMEOUT_S}s sin imagen (prompt {prompt_id})"}
            await asyncio.sleep(COMFY_POLL_INTERVAL_S)
        img = outs[0]
        v = await client.get(
            f"http://127.0.0.1:{port}/view",
            params={"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")},
        )
        v.raise_for_status()
        fname = f"{job_id}.png"
        dst = os.path.join(OUTPUT_DIR, fname)
        with open(dst, "wb") as f:
            f.write(v.content)
        logger.warning("comfyui t2i job=%s done file=%s", job_id, dst)
        # texto exacto encima del fondo (fiel al 100%, sin importar idioma)
        overlays = payload.get("overlays") or []
        if overlays:
            try:
                apply_text_overlays(dst, overlays)
            except Exception as exc:
                logger.warning("overlays failed job=%s err=%s — se entrega el fondo igual", job_id, exc)
        messages = payload.get("messages") or []
        if messages:
            try:
                apply_chat_messages(dst, messages)
            except Exception as exc:
                logger.warning("chat failed job=%s err=%s — se entrega igual", job_id, exc)
        return {"file": dst, "seed": seed, "comfyui_file": img["filename"], "overlays": len(overlays), "messages": len(messages)}


def _photo_to_file(photo: str, job_id: str) -> str | None:
    """Guarda foto base (data-url jpg/png/webp) como archivo del job."""
    import base64
    if not photo or "," not in photo:
        return None
    header, _, b64 = photo.partition(",")
    if "base64" not in header:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if not (raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"\x89PNG") or raw.startswith(b"RIFF")):
        return None
    dst = os.path.join(OUTPUT_DIR, f"{job_id}-base.png")
    with open(dst, "wb") as f:
        f.write(raw)
    return dst


async def _run_job_async(job_id: str, kind: str, payload: dict, job_queue: JobQueue | None = None) -> None:
    """Async worker body — switch VRAM model, mark completed/failed in Redis."""
    jobs = job_queue or JobQueue()
    # cancelado entre el enqueue y el arranque del worker: no revivirlo
    try:
        cur0 = jobs.get(job_id)
    except Exception:
        cur0 = None
    if cur0 is not None and cur0.get("status") not in ("queued", "running"):
        logger.warning("job %s ya no esta activo al arrancar (%s) — no se corre", job_id, cur0.get("status"))
        return
    try:
        jobs.update_status(job_id, "running")
    except Exception as exc:
        logger.warning("job %s update to running failed: %s", job_id, exc)
        return

    target = _target_for_kind(kind)
    # foto base SIN restyle: no necesita GPU ni switch — fondo tal cual + overlays encima.
    # Instantáneo y no voltea el modelo activo (ideal para avisos sobre fotos).
    if kind == "image" and payload.get("photo") and not payload.get("restyle"):
        result_payload = {"target": target, "payload": {k: v for k, v in payload.items() if k != "photo"}}
        try:
            cur = jobs.get(job_id)
        except Exception:
            cur = None
        if cur is not None and cur.get("status") not in ("running", "queued"):
            logger.warning("job %s ya no esta activo — no se genera", job_id)
            return
        base = _photo_to_file(payload["photo"], job_id)
        if base is None:
            jobs.update_status(job_id, "failed", error="foto inválida (jpg/png/webp en base64)")
            _notify_telegram(job_id, kind, "failed", payload, error="foto inválida")
            return
        for fn in (apply_text_overlays, apply_chat_messages):
            try:
                if fn is apply_text_overlays:
                    fn(base, payload.get("overlays") or [])
                else:
                    fn(base, payload.get("messages") or [])
            except Exception as exc:
                logger.warning("photo overlay failed job=%s err=%s", job_id, exc)
        result_payload["file"] = base
        _move_file_to_output(job_id, result_payload)
        jobs.update_status(job_id, "completed", result=result_payload)
        _notify_telegram(job_id, kind, "completed", result_payload)
        return
    try:
        orchestrator, registry = _get_orchestrator()
        # validate target in registry (raises if unknown)
        registry.resolve(target)
        ok = await orchestrator.switch_to(target)
        if not ok:
            jobs.update_status(job_id, "failed", error="model load timeout")
            _notify_telegram(job_id, kind, "failed", payload, error="model load timeout (240s)")
            return
        # switch succeeded — call backend if available (skip real generation in tests)
        result_payload = {"target": target, "payload": payload}
        # si el usuario lo cancelo mientras esperaba el switch, no generar.
        # OJO: el heartbeat va DESPUES de este chequeo, para no pisar su failed.
        try:
            cur = jobs.get(job_id)
        except Exception:
            cur = None
        if cur is not None and cur.get("status") not in ("running", "queued"):
            logger.warning("job %s ya no esta activo (%s) — no se genera", job_id, cur.get("status"))
            return
        try:
            jobs.update_status(job_id, "running", progress=60)
        except Exception:
            pass
        # skip heavy backend call during pytest to keep tests fast
        if not os.getenv("PYTEST_CURRENT_TEST"):
            try:
                if kind == "video":
                    # call wan-video :8189 /generate (480x832, 17 frames, 20 steps.
                    # 17 frames (4n+1, ~1s) es el maximo seguro en 3090 24GB: con 33
                    # el DiT entero sube a GPU de una (self.model.to(device)) y OOMea
                    # (2026-10-04: 23.5GB usados, 8MB libres). Ver README "Wan 14B".
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    async with httpx.AsyncClient(timeout=1200) as client:
                        gen_payload = {
                            "prompt": payload.get("prompt", ""),
                            "height": payload.get("height", 480),
                            "width": payload.get("width", 832),
                            "num_frames": payload.get("num_frames", 17),
                            "steps": payload.get("steps", 20),
                        }
                        r = await client.post(f"http://127.0.0.1:{port}/generate", json=gen_payload)
                        r.raise_for_status()
                        backend_result = r.json()
                        result_payload.update(backend_result)
                elif kind == "i2v":
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    async with httpx.AsyncClient(timeout=1200) as client:
                        gen_payload = {
                            "prompt": payload.get("prompt", ""),
                            "image": payload.get("image", ""),
                            "height": payload.get("height", 480),
                            "width": payload.get("width", 832),
                            "num_frames": payload.get("num_frames", 17),
                            "steps": payload.get("steps", 20),
                        }
                        r = await client.post(f"http://127.0.0.1:{port}/generate", json=gen_payload)
                        r.raise_for_status()
                        result_payload.update(r.json())
                elif kind == "avatar":
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    async with httpx.AsyncClient(timeout=1200) as client:
                        gen_payload = {
                            "image": payload.get("image", ""),
                            "audio": payload.get("audio", ""),
                            "prompt": payload.get("prompt", ""),
                        }
                        r = await client.post(f"http://127.0.0.1:{port}/generate", json=gen_payload)
                        r.raise_for_status()
                        result_payload.update(r.json())
                elif kind == "image":
                    spec = registry.resolve(target)
                    port = int(spec["port"])
                    if payload.get("photo") and payload.get("restyle"):
                        # restyle: la foto se redibuja "similar pero nueva" (img2img).
                        # El texto sale deformado a propósito: los finales los pega PIL.
                        base0 = _photo_to_file(payload["photo"], job_id + "-src")
                        if base0 is None:
                            result_payload["backend_error"] = "foto inválida (jpg/png/webp en base64)"
                        else:
                            gen = await _run_comfyui_img2img(port, payload, job_id, base0)
                            if gen.get("file"):
                                result_payload.update(gen)
                            else:
                                result_payload["backend_error"] = gen.get("backend_error", "comfyui sin imagen")
                    else:
                        # Qwen-Image text-to-image (foto fija con texto) vía ComfyUI.
                        gen = await _run_comfyui_t2i(port, payload, job_id)
                        if gen.get("file"):
                            result_payload.update(gen)
                        else:
                            result_payload["backend_error"] = gen.get("backend_error", "comfyui sin imagen")
            except Exception as be:
                logger.warning(f"backend call failed for {job_id} ({kind}): {be} — returning echo payload")
                result_payload["backend_error"] = str(be)

        # mueve archivos generados al disco grande si hace falta
        _move_file_to_output(job_id, result_payload)

        # si no hay archivo y no es un job de tipo que no genera archivo, marcar como failed
        has_file = bool(result_payload.get("file")) or any(
            isinstance(v, str) and v and os.path.exists(v)
            for k in ("file", "video_path", "image_path", "output", "path")
            for v in [result_payload.get(k)]
        )
        # también busca en payload anidado
        if not has_file:
            for v in result_payload.values():
                if isinstance(v, dict) and isinstance(v.get("file"), str) and os.path.exists(v["file"]):
                    has_file = True
                    break
        if not has_file and kind in ("video", "i2v", "avatar", "image"):
            # si el backend no devolvió file y tampoco hay backend_error, es fallo
            err = result_payload.get("backend_error") or "backend no devolvió archivo (¿VRAM/OOM/modelo no cargado?)"
            jobs.update_status(job_id, "failed", error=err, result=result_payload)
            _notify_telegram(job_id, kind, "failed", result_payload, error=err)
            return

        # si el usuario lo cancelo mientras generaba, no pisar su failed
        try:
            cur = jobs.get(job_id)
        except Exception:
            cur = None
        if cur is not None and cur.get("status") not in ("running", "queued"):
            logger.warning("job %s cancelado durante la generacion — se descarta el resultado", job_id)
            return

        jobs.update_status(job_id, "completed", result=result_payload)
        _notify_telegram(job_id, kind, "completed", result_payload)
    except asyncio.CancelledError:
        # shutdown/restart con el job en vuelo: sin esto queda zombie "running"
        # para siempre (los BackgroundTasks mueren en silencio).
        logger.warning("job %s cancelado con el worker en vuelo", job_id)
        try:
            jobs.update_status(job_id, "failed", error="worker detenido a mitad del job (restart/shutdown) — reenviá")
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.exception("job %s failed: %s", job_id, exc)
        try:
            jobs.update_status(job_id, "failed", error=str(exc))
        except Exception:
            pass
        _notify_telegram(job_id, kind, "failed", payload, error=str(exc))


def run_video_job(job_id: str, queue: JobQueue | None = None) -> None:
    """Legacy sync entrypoint — kept for backwards compat, now delegates to async worker."""
    import asyncio

    jobs = queue or JobQueue()
    job = jobs.get(job_id)
    payload = job.get("payload", {}) if job else {}
    try:
        asyncio.run(_run_job_async(job_id, "video", payload, jobs))
    except RuntimeError:
        # already in event loop (e.g. called from FastAPI BackgroundTasks) — schedule
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(asyncio.run, _run_job_async(job_id, "video", payload, jobs)).result()


def run_image_job(job_id: str, queue: JobQueue | None = None) -> None:
    """Sync entrypoint for image jobs (mirrors run_video_job)."""
    import asyncio

    jobs = queue or JobQueue()
    job = jobs.get(job_id)
    payload = job.get("payload", {}) if job else {}
    try:
        asyncio.run(_run_job_async(job_id, "image", payload, jobs))
    except RuntimeError:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(asyncio.run, _run_job_async(job_id, "image", payload, jobs)).result()

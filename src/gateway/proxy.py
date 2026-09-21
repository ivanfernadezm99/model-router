"""httpx streaming proxy — 600s timeout para LLMs lentos (30b + contexto grande), bearer passthrough, 500/504 verbatim."""

import json
import logging
import time
import uuid

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from src.common.notify import notify_error

logger = logging.getLogger(__name__)

TIMEOUT_S = 600
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


def _redact(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key"):
            out[k] = "Bearer ***" if "bearer" in v.lower() else "***"
        else:
            out[k] = v
    return out


async def proxy_request(request: Request, target_port: int, retry_after: int | None = None, task: str | None = None) -> Response | StreamingResponse:
    """Proxy request to http://127.0.0.1:{target_port}{path} preserving method, headers, streaming."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
    start = time.monotonic()
    # task-aware sampler: si viene de app.py usamos ese task, sino fallback a header
    _task_hint = (task or request.headers.get("x-model-hint", "") or "").strip().lower()
    _is_code = _task_hint in ("", "code")  # default code, pero si detector dijo code -> es code
    # Heurística texto vs código: si el contenido no parece código, forzar modo texto para no pegar palabras
    # Esto corrige el caso "email en español" que hoy cae en default:code y sufre DRY agresivo
    def _looks_like_code(text: str) -> bool:
        if not text:
            return True  # fallback conservador: asumir código
        lower = text.lower()
        code_markers = ("```", "def ", "import ", "class ", "function ", "const ", "let ", "var ", "npm ", "pip ", "cargo ", "select ", "from ", "where ", "curl ", "git ", "docker ", "{\n", ";\n")
        if any(m in lower for m in code_markers):
            return True
        # si tiene mucho texto natural español sin símbolos de código -> texto
        text_markers = ("estimado", "postulación", "atentamente", "experiencia", "integración", "hola", "asunto:", "ingeniero", "criterium", "landing")
        if any(m in lower for m in text_markers):
            return False
        # fallback: si es largo y sin marcadores de código, tratar como texto
        if len(text) > 200 and text.count("\n") < 5 and " " in text:
            # conteo simple: si predominan palabras pegadas sin estructura de código
            return False
        return True

    # build target url — only loopback allowed
    path = request.url.path
    qs = f"?{request.url.query}" if request.url.query else ""
    url = f"http://127.0.0.1:{target_port}{path}{qs}"

    # forward headers except hop-by-hop, preserve bearer
    fwd_headers = {}
    for k, v in request.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        if k.lower() == "host":
            continue
        fwd_headers[k] = v

    body = await request.body()

    # --- sampler anti-loop + determinístico (190K YaRN 5.9x) — sin margen para alucinar ---
    # Fixes: n_tokens=65961 loop at 20t/s. 190K necesita temp baja para no alucinar con YaRN alto.
    # FIX palabras pegadas: DRY con allowed_length=2 es agresivo para texto natural (español) y se come espacios
    # para evitar bigrama " de", " en". Solo aplicar DRY agresivo a task=code; para texto usar modo suave.
    try:
        ctype_req = request.headers.get("content-type", "")
        if "application/json" in ctype_req and "/v1/" in path and body:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and ("prompt" in parsed or "messages" in parsed):
                # re-evaluar _is_code con contenido real si el detector dijo "code" por default
                try:
                    _content_probe = ""
                    if "prompt" in parsed and isinstance(parsed["prompt"], str):
                        _content_probe = parsed["prompt"]
                    elif "messages" in parsed and isinstance(parsed["messages"], list) and parsed["messages"]:
                        # último mensaje user es el que más importa
                        for m in reversed(parsed["messages"]):
                            if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
                                _content_probe = m["content"]
                                break
                        if not _content_probe:
                            _content_probe = str(parsed["messages"][-1].get("content", "")) if isinstance(parsed["messages"][-1], dict) else ""
                    if _content_probe and not _looks_like_code(_content_probe):
                        _is_code = False
                        _task_hint = "text"
                except Exception:
                    pass
                patched = {}
                rp = parsed.get("repeat_penalty")
                if rp is None or rp == 1.0:
                    parsed["repeat_penalty"] = 1.15
                    patched["repeat_penalty"] = 1.15
                if "repeat_last_n" not in parsed:
                    parsed["repeat_last_n"] = 256
                    patched["repeat_last_n"] = 256
                # DRY: solo para código; para texto deshabilitar o usar allowed_length=8 suave
                if _is_code:
                    if "dry_multiplier" not in parsed or parsed.get("dry_multiplier") == 0:
                        parsed["dry_multiplier"] = 0.9
                        parsed["dry_base"] = 1.75
                        parsed["dry_allowed_length"] = 2
                        parsed["dry_penalty_last_n"] = 512
                        patched["dry_multiplier"] = 0.9
                        patched["dry_task"] = "code"
                else:
                    # texto natural: desactivar DRY agresivo, usar allowed_length=8 si viene seteado
                    if parsed.get("dry_multiplier", 0) != 0:
                        # si el caller ya pidió DRY, suavizarlo
                        if parsed.get("dry_allowed_length", 2) < 8:
                            parsed["dry_allowed_length"] = 8
                            patched["dry_allowed_length"] = "softened_to_8_for_text"
                    else:
                        # explícitamente deshabilitar DRY para texto
                        parsed["dry_multiplier"] = 0
                        patched["dry_multiplier"] = "disabled_for_text"
                # Temperatura: code -> determinística 0.2, texto -> 0.4-0.6 más natural
                t = parsed.get("temperature")
                if _is_code:
                    if t is None:
                        parsed["temperature"] = 0.2
                        patched["temperature"] = 0.2
                    elif t > 0.3:
                        parsed["temperature"] = 0.2
                        patched["temperature"] = "capped_0.2(from %.2f)" % t
                else:
                    if t is None:
                        parsed["temperature"] = 0.4
                        patched["temperature"] = 0.4
                    elif t > 0.7:
                        parsed["temperature"] = 0.6
                        patched["temperature"] = "capped_0.6(from %.2f)" % t
                    elif t < 0.2:
                        parsed["temperature"] = 0.4
                        patched["temperature"] = "raised_0.4(from %.2f)" % t
                if "top_p" not in parsed or parsed.get("top_p") > 0.9:
                    # top_p alto + YaRN = alucinación, cap a 0.85
                    old = parsed.get("top_p")
                    parsed["top_p"] = 0.85
                    patched["top_p"] = "capped_0.85(from %s)" % str(old)
                if "top_k" not in parsed:
                    parsed["top_k"] = 20
                    patched["top_k"] = 20
                elif parsed.get("top_k", 0) > 40:
                    oldk = parsed.get("top_k")
                    parsed["top_k"] = 20
                    patched["top_k"] = "capped_20(from %s)" % str(oldk)
                if patched:
                    body = json.dumps(parsed).encode()
                    if "content-length" in fwd_headers:
                        fwd_headers["content-length"] = str(len(body))
                    logger.info(json.dumps({"request_id": request_id, "sampler_patch": True, "patched": patched, "target": target_port, "task": _task_hint or "code"}))
    except Exception as e:
        logger.warning(json.dumps({"request_id": request_id, "sampler_patch_error": str(e)}))

    # structured log (redacted)
    hint = request.headers.get("x-model-hint", "")
    target_model = f"port:{target_port}"

    client = httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)
    try:
        # stream request body
        req = client.build_request(request.method, url, headers=fwd_headers, content=body)

        resp = await client.send(req, stream=True)

        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info(json.dumps({
            "request_id": request_id,
            "hint": hint,
            "target_model": target_model,
            "queue_depth": 0,
            "latency_ms": latency_ms,
            "status": resp.status_code,
            "headers": _redact(dict(fwd_headers)),
        }))

        # notify on backend 5xx — visible in Telegram + file log
        if resp.status_code >= 500:
            notify_error(
                f"Backend 5xx port {target_port}",
                f"request_id={request_id} status={resp.status_code} hint={hint} target={target_model}",
            )

        # passthrough 500/502/504 verbatim, and any status
        # stream if backend is event-stream or request had stream:true
        ctype = resp.headers.get("content-type", "")
        is_stream = "text/event-stream" in ctype or b'"stream": true' in body or b'"stream":true' in body

        # collect headers to forward (exclude hop-by-hop)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
        if retry_after is not None:
            out_headers["retry-after"] = str(retry_after)

        if is_stream:
            async def aiter():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                except httpx.ReadError as exc:
                    # client (opencode) cancelled mid-stream — log + Telegram (throttled)
                    logger.warning(json.dumps({"request_id": request_id, "error": f"stream_read_error:{exc}", "target": target_port}))
                    notify_error("Stream cortado (opencode cancel)", f"request_id={request_id} target={target_port} hint={hint} — stream ReadError: {exc}")
                except Exception as exc:
                    logger.warning(json.dumps({"request_id": request_id, "error": f"stream_error:{exc}", "target": target_port}))
                finally:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    try:
                        await client.aclose()
                    except Exception:
                        pass

            return StreamingResponse(aiter(), status_code=resp.status_code, headers=out_headers, media_type=ctype or "text/event-stream")

        # buffered non-stream — read fully then return
        content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        # ensure content-type preserved
        return Response(content=content, status_code=resp.status_code, headers=out_headers, media_type=ctype or "application/json")

    except httpx.ReadTimeout:
        try:
            await client.aclose()
        except Exception:
            pass
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": "timeout", "latency_ms": latency_ms})
        logger.warning(msg)
        notify_error("Gateway timeout", f"request_id={request_id} target_port={target_port} latency={latency_ms}ms — httpx.ReadTimeout")
        return Response(content=json.dumps({"error": "model load timeout"}), status_code=504, media_type="application/json")
    except httpx.ConnectError:
        try:
            await client.aclose()
        except Exception:
            pass
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": "connect", "latency_ms": latency_ms})
        logger.warning(msg)
        notify_error("Backend no conecta", f"request_id={request_id} target_port={target_port} — httpx.ConnectError")
        return Response(content=json.dumps({"error": "backend unavailable"}), status_code=502, media_type="application/json")
    except httpx.ReadError as exc:
        try:
            await client.aclose()
        except Exception:
            pass
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": f"read_error:{exc}", "latency_ms": latency_ms})
        logger.warning(msg)
        notify_error("Cliente cortó conexión", f"request_id={request_id} target_port={target_port} hint={hint} — httpx.ReadError: {exc} (opencode canceló 40k tokens?)")
        return Response(content=json.dumps({"error": "client disconnected"}), status_code=499, media_type="application/json")
    except Exception as exc:
        try:
            await client.aclose()
        except Exception:
            pass
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": str(exc), "latency_ms": latency_ms})
        logger.error(msg)
        notify_error("Gateway proxy error", f"request_id={request_id} target_port={target_port} — {exc}")
        return Response(content=json.dumps({"error": "proxy error"}), status_code=500, media_type="application/json")

"""httpx streaming proxy — 600s timeout para LLMs lentos (30b + contexto grande), bearer passthrough, 500/504 verbatim."""

import json
import logging
import os
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
    # Text vs code: decides whether the aggressive anti-loop defaults apply.
    # Requires positive code evidence. The previous version defaulted to code
    # and carried a hardcoded Spanish word list ("criterium", "landing") added
    # for one past incident — so any short message ("escribime un mail") was
    # classified as code and got temp 0.2 + DRY 0.9 + top_k 20. Guessing
    # "code" by default means guessing wrong for everything short.
    # Loop protection for real runaway generations belongs to LoopGuardMiddleware.
    def _looks_like_code(text: str) -> bool:
        if not text:
            return False
        lower = text.lower()
        code_markers = ("```", "def ", "import ", "class ", "function ", "const ", "let ", "var ", "npm ", "pip ", "cargo ", "select ", "from ", "where ", "curl ", "git ", "docker ", "{\n", ";\n", "()", "=>", "</", "/>", "#!/")
        return any(m in lower for m in code_markers)

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

    # --- sampler defaults ---
    #
    # Original intent: stop the n_tokens=65961 loop that showed up at 20t/s on
    # 190K YaRN 5.9x. That goal was met by unconditionally rewriting the client's
    # sampling on every /v1 request (1424/1424 in journalctl, identical patch).
    #
    # That was wrong for agent traffic. A tool call has to emit a file path
    # character for character, and a path IS repetition: "/home", "servidor",
    # ".py" repeat inside a single path and across consecutive tool calls.
    # DRY with dry_allowed_length=2 penalises exactly that, so the sampler was
    # fighting the model on the one task that needs precision most.
    #
    # Two rules now:
    #   1. Never override a value the client set. A proxy that rewrites
    #      explicit intent is not a proxy. Defaults only fill absent keys.
    #   2. Agent traffic (the request carries `tools`) gets repetition-friendly
    #      defaults, because its output is paths and JSON, not prose.
    # Anti-loop settings stay for non-agent code traffic, which is where the
    # loop actually came from.
    #
    # SAMPLER_DEFAULTS_DISABLED=1 turns the whole block off.
    _sampler_off = os.environ.get("SAMPLER_DEFAULTS_DISABLED", "0") == "1"
    try:
        ctype_req = request.headers.get("content-type", "")
        if not _sampler_off and "application/json" in ctype_req and "/v1/" in path and body:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and ("prompt" in parsed or "messages" in parsed):
                is_agent = bool(parsed.get("tools"))
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

                def _default(key, value):
                    """Set only if the client did not specify it. Never override."""
                    if key not in parsed or parsed.get(key) is None:
                        parsed[key] = value
                        patched[key] = value

                # --- repetition control ---
                # Agent output is paths and JSON: mild penalty, wide window.
                # Non-agent code keeps the stronger penalty that killed the loop.
                _default("repeat_penalty", 1.05 if is_agent else 1.15)
                _default("repeat_last_n", 256)

                # --- DRY ---
                # Aggressive DRY is only for non-agent code. On agent traffic
                # it stays off unless the client asked for it, because DRY
                # penalises the repeated substrings that make up a file path.
                if _is_code and not is_agent:
                    if not parsed.get("dry_multiplier"):
                        parsed["dry_multiplier"] = 0.9
                        parsed["dry_base"] = 1.75
                        parsed["dry_allowed_length"] = 2
                        parsed["dry_penalty_last_n"] = 512
                        patched["dry_multiplier"] = 0.9
                        patched["dry_task"] = "code"
                elif not _is_code and not is_agent and parsed.get("dry_multiplier"):
                    # prose asked for DRY: soften so it stops eating spaces.
                    # Never for agent traffic — rule 1 is absolute.
                    if parsed.get("dry_allowed_length", 2) < 8:
                        parsed["dry_allowed_length"] = 8
                        patched["dry_allowed_length"] = "softened_to_8_for_text"

                # --- temperature ---
                _default("temperature", 0.2 if _is_code else 0.6)

                # --- nucleus / top-k ---
                # Defaults only. The old code capped whatever the client sent,
                # which is how an explicit top_p of 0.95 became 0.85.
                _default("top_p", 0.85 if (_is_code and not is_agent) else 0.9)
                # top_k truncates the tail hard; agent output needs that tail
                # to finish a long path token by token.
                if not is_agent:
                    _default("top_k", 20)

                if patched:
                    body = json.dumps(parsed).encode()
                    if "content-length" in fwd_headers:
                        fwd_headers["content-length"] = str(len(body))
                    logger.info(json.dumps({"request_id": request_id, "sampler_defaults": True, "patched": patched, "agent": is_agent, "target": target_port, "task": _task_hint or "code"}))
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

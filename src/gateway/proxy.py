"""httpx streaming proxy — 240s timeout, bearer passthrough, 500/504 verbatim."""

import json
import logging
import time
import uuid

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from src.common.notify import notify_error

logger = logging.getLogger(__name__)

TIMEOUT_S = 240
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


def _redact(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key"):
            out[k] = "Bearer ***" if "bearer" in v.lower() else "***"
        else:
            out[k] = v
    return out


async def proxy_request(request: Request, target_port: int, retry_after: int | None = None) -> Response | StreamingResponse:
    """Proxy request to http://127.0.0.1:{target_port}{path} preserving method, headers, streaming."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
    start = time.monotonic()

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

    # structured log (redacted)
    hint = request.headers.get("x-model-hint", "")
    target_model = f"port:{target_port}"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
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
                    finally:
                        await resp.aclose()

                return StreamingResponse(aiter(), status_code=resp.status_code, headers=out_headers, media_type=ctype or "text/event-stream")

            # buffered non-stream — read fully then return
            content = await resp.aread()
            await resp.aclose()
            # ensure content-type preserved
            return Response(content=content, status_code=resp.status_code, headers=out_headers, media_type=ctype or "application/json")

    except httpx.ReadTimeout:
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": "timeout", "latency_ms": latency_ms})
        logger.warning(msg)
        notify_error("Gateway timeout", f"request_id={request_id} target_port={target_port} latency={latency_ms}ms — httpx.ReadTimeout")
        return Response(content=json.dumps({"error": "model load timeout"}), status_code=504, media_type="application/json")
    except httpx.ConnectError:
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": "connect", "latency_ms": latency_ms})
        logger.warning(msg)
        notify_error("Backend no conecta", f"request_id={request_id} target_port={target_port} — httpx.ConnectError")
        return Response(content=json.dumps({"error": "backend unavailable"}), status_code=502, media_type="application/json")
    except httpx.ReadError as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": f"read_error:{exc}", "latency_ms": latency_ms})
        logger.warning(msg)
        notify_error("Cliente cortó conexión", f"request_id={request_id} target_port={target_port} hint={hint} — httpx.ReadError: {exc} (opencode canceló 40k tokens?)")
        return Response(content=json.dumps({"error": "client disconnected"}), status_code=499, media_type="application/json")
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        msg = json.dumps({"request_id": request_id, "error": str(exc), "latency_ms": latency_ms})
        logger.error(msg)
        notify_error("Gateway proxy error", f"request_id={request_id} target_port={target_port} — {exc}")
        return Response(content=json.dumps({"error": "proxy error"}), status_code=500, media_type="application/json")

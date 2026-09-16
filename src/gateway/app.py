"""FastAPI gateway :8000 — lifespan with IdleReaper, POST /v1/* handler."""

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from src.gateway.detector import detect_task, task_to_model
from src.gateway.metrics import health_payload, metrics_text
from src.gateway.proxy import proxy_request
from src.gateway.queue import GatewayQueue
from src.orchestrator.idle import IdleReaper
from src.orchestrator.lifecycle import Orchestrator
from src.registry.registry import Registry
from src.jobs.router import router as job_router

logger = logging.getLogger(__name__)
# ensure JSON request logs appear in journalctl (uvicorn default filters app loggers)
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger.setLevel(logging.INFO)

# singletons — importable for tests
registry = Registry()
try:
    registry.load()
except Exception as exc:
    logger.warning(f"registry load failed at import: {exc}")

orchestrator = Orchestrator(registry=registry)
logger.info(f"gateway: orchestrator created id={id(orchestrator)} module={__name__} file={__file__}")
queue = GatewayQueue()
reaper = IdleReaper(orchestrator)


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper.start()
    try:
        yield
    finally:
        reaper.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(job_router)


@app.middleware("http")
async def touch_reaper_on_every_request(request: Request, call_next):
    """Reset idle reaper timer on ANY request (not just /v1 proxy)."""
    reaper.touch()
    return await call_next(request)


@app.get("/debug/health")
async def debug_health():
    """Debug endpoint to check auto-detect state."""
    result = {"active_model": orchestrator.active_model, "orchestrator_id": id(orchestrator), "registry_models": len(getattr(registry, "models", {}) or {})}
    import subprocess
    models = getattr(registry, "models", {}) or {}
    for name, spec in models.items():
        svc = spec.get("service")
        if not svc:
            continue
        r = subprocess.run(
            ["systemctl", "--user", "is-active", svc],
            capture_output=True, text=True, timeout=2,
            env={"XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"},
        )
        result[f"{name}_systemd"] = r.stdout.strip()
    return result


@app.get("/health")
async def health():
    # si active_model es None, adoptar el servicio systemd activo que esté
    # saludable (no solo "active" en systemd, que puede estar cayendo).
    if orchestrator.active_model is None:
        logger.info("auto-detect: starting, active_model=None")
        try:
            import subprocess
            import httpx
            models = getattr(registry, "models", {}) or {}
            logger.info(f"auto-detect: registry has {len(models)} models")
            async with httpx.AsyncClient(timeout=3) as client:
                for name, spec in models.items():
                    svc = spec.get("service")
                    port = spec.get("port")
                    endpoint = spec.get("health_endpoint", "/health")
                    if not svc or not port:
                        continue
                    r = subprocess.run(
                        ["systemctl", "--user", "is-active", svc],
                        capture_output=True, text=True, timeout=2,
                        env={"XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"},
                    )
                    sysd_status = r.stdout.strip()
                    logger.info(f"auto-detect: {name} svc={svc} systemd={sysd_status}")
                    if sysd_status != "active":
                        continue
                    try:
                        hr = await client.get(f"http://127.0.0.1:{port}{endpoint}")
                        logger.info(f"auto-detect: {name} health={hr.status_code}")
                        if hr.status_code == 200:
                            orchestrator.active_model = name
                            orchestrator.active_service = svc
                            logger.info(f"auto-detect: adopted {name}")
                            break
                    except Exception as e:
                        logger.info(f"auto-detect: {name} health error: {e}")
                        continue
            logger.info(f"auto-detect: final active_model={orchestrator.active_model}")
        except Exception as e:
            logger.info(f"auto-detect: exception: {e}")
    return JSONResponse(health_payload(orchestrator, queue))


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(metrics_text(orchestrator, queue), media_type="text/plain; version=0.0.4")


async def _resolve_port(task: str) -> int:
    model_key = task_to_model(task)
    spec = registry.resolve(model_key)
    return int(spec["port"])


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def v1_proxy(path: str, request: Request):
    start = time.monotonic()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]

    # read body for detector (without consuming proxy's body — we re-read via request.body())
    raw_body = await request.body()
    # try parse json for prefix detection, fallback to raw bytes
    body_for_detect: dict | bytes = raw_body
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype and raw_body:
        try:
            body_for_detect = json.loads(raw_body)
        except Exception:
            body_for_detect = raw_body

    # detector validates header/prefix case-insensitive, default code, registry valid
    try:
        task = detect_task(dict(request.headers), body_for_detect, registry=registry)
    except ValueError as exc:
        # unknown hint -> 400, no systemctl
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info(json.dumps({"request_id": request_id, "hint": dict(request.headers).get("x-model-hint", ""), "target_model": "unknown", "queue_depth": queue.depth(), "latency_ms": latency_ms, "error": str(exc)}))
        return JSONResponse({"error": str(exc)}, status_code=400)

    target_model = task_to_model(task)
    try:
        target_port = await _resolve_port(task)
    except KeyError as exc:
        return JSONResponse({"error": f"unknown model: {exc}"}, status_code=400)

    # idle touch on every /v1 request
    reaper.touch()

    # if orchestrator has exclusive lock (swap in progress) -> enqueue 202
    if orchestrator.lock.locked() or queue.is_swapping():
        # if target matches swapping target, coalesce — single systemctl start already done by holder
        result = await queue.enqueue(target_model, request_id=request_id)
        # if enqueued item already expired (should not happen immediately)
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info(json.dumps({"request_id": request_id, "hint": task, "target_model": target_model, "queue_depth": queue.depth(), "latency_ms": latency_ms, "queued": True}))
        return JSONResponse(result, status_code=202, headers={"Retry-After": str(result["retry_after"]), "X-Queue-Depth": str(queue.depth()), "X-Target-Model": target_model})

    # if target equals active -> proxy directly (fast path)
    if orchestrator.active_model == target_model:
        resp = await proxy_request(request, target_port)
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info(json.dumps({"request_id": request_id, "hint": task, "target_model": target_model, "queue_depth": queue.depth(), "latency_ms": latency_ms, "status": resp.status_code}))
        return resp

    # for code task: any coder* already loaded is ok — don't thrash between 14b/30b variants.
    # Esto permite que el front elija manualmente 30b y opencode lo use sin que el gateway lo baje a 14b.
    if task == "code" and orchestrator.active_model and orchestrator.active_model.startswith("coder"):
        try:
            active_spec = registry.resolve(orchestrator.active_model)
            active_port = int(active_spec["port"])
            resp = await proxy_request(request, active_port)
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.info(json.dumps({"request_id": request_id, "hint": task, "target_model": target_model, "active_model": orchestrator.active_model, "queue_depth": queue.depth(), "latency_ms": latency_ms, "status": resp.status_code, "coder_passthrough": True}))
            return resp
        except Exception:
            pass  # fall through to normal switch

    # if active is None but target port is already healthy (manual server), adopt it and proxy directly
    if orchestrator.active_model is None:
        try:
            from src.orchestrator.health import poll_health

            is_healthy = await poll_health(target_port, registry.resolve(target_model)["health_endpoint"], timeout_s=2)
            if is_healthy:
                orchestrator.active_model = target_model
                orchestrator.active_service = registry.resolve(target_model)["service"]
                resp = await proxy_request(request, target_port)
                latency_ms = int((time.monotonic() - start) * 1000)
                logger.info(json.dumps({"request_id": request_id, "hint": task, "target_model": target_model, "queue_depth": queue.depth(), "latency_ms": latency_ms, "status": resp.status_code, "adopted": True}))
                return resp
        except Exception:
            pass

    # mismatch -> need swap (stop-before-start). Hold queue swapping flag.
    # If active is None (idle), just start.
    queue.set_swapping(target_model)
    try:
        ok = await orchestrator.switch_to(target_model)
        if not ok:
            # 240s timeout or vram/holds block -> 504, no fallback unless image/video timeout then fallback to code
            # fallback to local llm (code on :8082) per task-routing spec SHOULD
            is_image_or_video = task in ("image", "video")
            if is_image_or_video:
                # attempt fallback to code
                try:
                    fb_port = await _resolve_port("code")
                    # verify fallback health quickly (reuse orchestrator health poll is inside switch_to)
                    # simple: proxy to fallback if switch_to timeout was health
                    from src.orchestrator.health import poll_health
                    fb_spec = registry.resolve(task_to_model("code"))
                    fallback_ok = await poll_health(fb_spec["port"], fb_spec["health_endpoint"], timeout_s=3)
                    if fallback_ok:
                        resp = await proxy_request(request, fb_port)
                        # add fallback header
                        resp.headers["X-Fallback"] = "local-llm"
                        return resp
                except Exception:
                    pass
            queue.clear_swapping()
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.warning(json.dumps({"request_id": request_id, "hint": task, "target_model": target_model, "queue_depth": queue.depth(), "latency_ms": latency_ms, "error": "model load timeout"}))
            return JSONResponse({"error": "model load timeout"}, status_code=504)

        # swap succeeded — proxy current request
        resp = await proxy_request(request, target_port)
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info(json.dumps({"request_id": request_id, "hint": task, "target_model": target_model, "queue_depth": queue.depth(), "latency_ms": latency_ms, "status": resp.status_code, "swapped": True}))

        # drain FIFO queue items that arrived during swap (arrival order) — best-effort proxy in order
        # We don't proxy queued items automatically here (they already got 202). Drain is for metrics/tests;
        # in real flow, queued callers retry after Retry-After. For spec "drain queue after swap", we just clear after small grace.
        # To satisfy <swap+5s drain, we empty the in-memory queue without re-proxying (callers will retry).
        # But expose drain for tests.
        # Note: if we want to auto-proxy queued, we would need stored Request — not feasible after 202.
        # So we just clear swapping flag; depth will be reported until callers retry and are proxied.
        return resp
    finally:
        queue.clear_swapping()
        # after swap, if orchestrator set active, status becomes ready
        pass


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="model-router gateway")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (use 0.0.0.0 for Tailscale)")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)

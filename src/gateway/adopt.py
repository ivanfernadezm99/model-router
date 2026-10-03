"""Reconcile orchestrator.active_model with what is actually running.

active_model is in-memory, so it starts as None on every gateway restart while
the model service keeps running untouched. Callers that act on it destructively
— POST /jobs/switch above all — then read a loaded model as absent and reload
it, paying a full ~100s load for nothing.

Systemd unit state is the discriminator, never the port: thirteen coder models
share port 8082, so a healthy port proves a model is loaded but never which one.
Port health is only the readiness gate, because a unit can be active while its
model is still loading (llama-server answers 503 until the weights land).
"""

import asyncio
import logging
import os
import subprocess

import httpx

logger = logging.getLogger(__name__)

SYSTEMD_TIMEOUT_S = 2
HEALTH_TIMEOUT_S = 3.0


def _systemd_env() -> dict:
    # derived, not hardcoded: a hardcoded /run/user/1000 silently reports every
    # unit inactive under any other account
    runtime = f"/run/user/{os.getuid()}"
    return {
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def systemd_active(service: str) -> bool:
    """True when the user unit reports active. Never raises."""
    if not service:
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", service],
            capture_output=True, text=True, timeout=SYSTEMD_TIMEOUT_S,
            env=_systemd_env(),
        )
    except Exception as exc:
        logger.warning(f"adopt: systemctl is-active {service} failed: {type(exc).__name__}: {exc}")
        return False
    return r.stdout.strip() == "active"


def _port_ready(port: int, endpoint: str) -> bool:
    """True when the port answers 200 — ready, not just bound."""
    if not port:
        return False
    try:
        r = httpx.get(f"http://127.0.0.1:{port}{endpoint or '/health'}", timeout=HEALTH_TIMEOUT_S)
    except Exception:
        return False
    return r.status_code == 200


async def probe_running_model(registry) -> dict | None:
    """Return {name, service, port} for the model actually running, else None.

    Ordered by evidence, cheapest first: systemd unit state narrows the field,
    then port health confirms readiness. When more than one unit is active the
    answer is genuinely ambiguous, so it is logged rather than guessed quietly.
    """
    models = getattr(registry, "models", {}) or {}
    candidates = []
    for name, spec in models.items():
        service = spec.get("service")
        if not service:
            continue
        if systemd_active(service):
            candidates.append((name, spec))

    if not candidates:
        return None

    if len(candidates) > 1:
        names = [n for n, _ in candidates]
        logger.warning(f"adopt: {len(candidates)} units active at once {names} — ambiguous, probing ports")

    ready = [
        (name, spec)
        for name, spec in candidates
        if _port_ready(spec.get("port"), spec.get("health_endpoint", "/health"))
    ]

    if not ready:
        # A unit can be active while weights are still loading. Not an error:
        # the caller keeps whatever it had and tries again later.
        logger.info(f"adopt: {len(candidates)} unit(s) active but no port ready yet {[n for n, _ in candidates]}")
        return None

    if len(ready) > 1:
        # Same port served by several unit names: nothing distinguishes them.
        logger.warning(f"adopt: several active units ready on the same port {[n for n, _ in ready]} — adopting first by config order")
    name, spec = ready[0]
    return {"name": name, "service": spec.get("service"), "port": spec.get("port")}


async def reconcile_active_model(orchestrator, registry) -> str | None:
    """Fill in active_model from reality when it is missing. Never overrides it.

    Only fills a None value: an active_model set by switch_to is the result of
    an exclusive lifecycle operation, and second-guessing it here would race
    with a swap in progress.
    """
    if orchestrator.active_model is not None:
        return orchestrator.active_model

    found = await probe_running_model(registry)
    if found is None:
        return None

    orchestrator.active_model = found["name"]
    orchestrator.active_service = found["service"]
    logger.info(f"adopt: reconciled active_model={found['name']} service={found['service']}")
    return found["name"]


async def reconcile_until_ready(orchestrator, registry, deadline_s: float = 300.0, interval_s: float = 5.0) -> str | None:
    """Keep retrying adoption until a model answers or the deadline passes.

    A one-shot reconcile at startup loses the race against a model that is still
    loading its weights, which is precisely when a stack restart happens. Without
    the retry the gateway stays blind for as long as nobody polls /health.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    attempts = 0
    while True:
        found = await reconcile_active_model(orchestrator, registry)
        if found is not None:
            if attempts:
                logger.info(f"adopt: succeeded after {attempts} retries")
            return found
        if loop.time() >= end:
            logger.warning(f"adopt: gave up after {attempts} attempts over {deadline_s:.0f}s with no ready model")
            return None
        attempts += 1
        await asyncio.sleep(interval_s)

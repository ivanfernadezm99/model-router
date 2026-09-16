"""Health polling — 2s interval, 240s hard timeout, 180s nominal budget."""

import asyncio
import logging

import httpx

from src.common.validation import validate_health

NOMINAL_TIMEOUT_S = 180
HARD_TIMEOUT_S = 240
POLL_INTERVAL_S = 2.0

logger = logging.getLogger(__name__)


async def poll_health(
    port: int,
    endpoint: str,
    timeout_s: int = HARD_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
) -> bool:
    """Poll http://127.0.0.1:{port}{endpoint} every interval_s until 200 or timeout.

    Returns True on first 200, False on timeout. Validates allowlist before polling.
    """
    validate_health(endpoint, port)
    url = f"http://127.0.0.1:{port}{endpoint}"
    deadline = asyncio.get_event_loop().time() + timeout_s

    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            if asyncio.get_event_loop().time() >= deadline:
                logger.warning("health timeout url=%s timeout=%s", url, timeout_s)
                return False
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True
            except Exception as exc:
                logger.debug("health poll error url=%s err=%s", url, exc)
            # check if remaining time < interval -> sleep remaining
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(interval_s, remaining))


def is_ready_status(status_code: int) -> bool:
    """True when the service reports ready. 503 = still loading (not a failure)."""
    return status_code == 200


def is_terminal_status(status_code: int) -> bool:
    """True when the service reports a permanent failure (not transient loading)."""
    return status_code >= 500 and status_code != 503

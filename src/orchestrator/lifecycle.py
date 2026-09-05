"""Exclusive systemd lifecycle — asyncio.Lock, stop-before-start, -np 1, holds, VRAM guard."""

import asyncio
import logging
import re
import subprocess

from src.common.validation import holds_ok, validate_np
from src.orchestrator.health import HARD_TIMEOUT_S, poll_health
from src.orchestrator.vram import check_vram_for_model, get_vram_used_mb

logger = logging.getLogger(__name__)

SERVICE_RE = re.compile(r"^[a-z0-9-]+\.service$")


def _validate_service(name: str) -> None:
    if not SERVICE_RE.match(name):
        raise ValueError(f"invalid service name: {name}")


def _run_systemctl(action: str, service: str) -> subprocess.CompletedProcess:
    _validate_service(service)
    if action not in ("stop", "start", "is-active"):
        raise ValueError(f"invalid action {action}")
    return subprocess.run(
        ["systemctl", "--user", action, service],
        capture_output=True,
        text=True,
        timeout=15,
    )


class Orchestrator:
    """Owns exclusive VRAM via asyncio.Lock + systemctl --user."""

    def __init__(self, registry=None):
        self.registry = registry
        self.lock = asyncio.Lock()
        self.active_model: str | None = None
        self.active_service: str | None = None

    def _check_holds(self) -> bool:
        a, b = holds_ok()
        if not (a and b):
            logger.warning("holds missing cuda=%s kornia=%s — blocking swap", a, b)
            return False
        return True

    async def stop_current(self) -> None:
        if self.active_service:
            _run_systemctl("stop", self.active_service)
            # poll VRAM until <2GB or short grace (best-effort, not blocking health)
            for _ in range(5):
                used = get_vram_used_mb()
                if used is not None and used < 2048:
                    break
                await asyncio.sleep(0.5)
            self.active_model = None
            self.active_service = None

    async def switch_to(self, target: str) -> bool:
        """Exclusive stop-before-start. Returns True on ready, False on timeout/block.

        Callers waiting on the same target coalesce via the lock — only one
        systemctl start is issued per swap.
        """
        if self.registry is None:
            raise RuntimeError("registry not configured")

        spec = self.registry.resolve(target)  # raises KeyError -> unknown model
        validate_np(spec.get("args", []))

        if not self._check_holds():
            return False

        can, used = check_vram_for_model(spec["vram_mb"])
        if not can:
            logger.warning("vram block target=%s needed=%s used=%s", target, spec["vram_mb"], used)
            # we still proceed to stop current first if active differs;
            # but if used is None (fail-closed) we block entirely
            if used is None:
                return False

        async with self.lock:
            # coalesce: if another waiter already completed this target
            if self.active_model == target:
                return True

            # stop current if different
            if self.active_service and self.active_model != target:
                _run_systemctl("stop", self.active_service)
                # wait VRAM reclaim briefly
                for _ in range(10):
                    used_now = get_vram_used_mb()
                    if used_now is not None and used_now < 2048:
                        break
                    await asyncio.sleep(0.5)

            # re-check VRAM after stop (fail-closed)
            can2, used2 = check_vram_for_model(spec["vram_mb"])
            if not can2:
                logger.warning("vram still blocked after stop target=%s used=%s", target, used2)
                if used2 is None:
                    return False
                # if still over budget after stop, block — exclusive lock should have freed it,
                # so this means spec itself exceeds 24GB
                if spec["vram_mb"] > 24 * 1024:
                    return False

            _run_systemctl("start", spec["service"])

            ok = await poll_health(spec["port"], spec["health_endpoint"], timeout_s=HARD_TIMEOUT_S)
            if ok:
                self.active_model = target
                self.active_service = spec["service"]
                return True

            # timeout -> rollback stop target
            logger.warning("health timeout target=%s", target)
            try:
                _run_systemctl("stop", spec["service"])
            except Exception:
                pass
            return False

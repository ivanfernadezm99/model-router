"""Idle reaper — 60s tick, 600s timeout, reset on activity, libre <2GB."""

import asyncio
import logging
import time

from src.orchestrator.vram import is_idle_vram_ok

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_S = 600
TICK_S = 60


class IdleReaper:
    def __init__(self, orchestrator, idle_timeout_s: int = IDLE_TIMEOUT_S, tick_s: int = TICK_S):
        self.orchestrator = orchestrator
        self.idle_timeout_s = idle_timeout_s
        self.tick_s = tick_s
        self.last_activity = time.monotonic()
        self._task: asyncio.Task | None = None
        self._running = False

    def touch(self) -> None:
        """Reset idle timer on any proxied request."""
        self.last_activity = time.monotonic()

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.tick_s)
            elapsed = time.monotonic() - self.last_activity
            if elapsed >= self.idle_timeout_s and self.orchestrator.active_model is not None:
                logger.info("idle reaper firing elapsed=%s model=%s", elapsed, self.orchestrator.active_model)
                try:
                    await self.orchestrator.stop_current()
                except Exception as exc:
                    logger.warning("idle stop failed err=%s", exc)
                    continue
                # verify VRAM <2GB after stop (best-effort)
                if not is_idle_vram_ok():
                    logger.warning("idle vram not reclaimed <2GB after stop")

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

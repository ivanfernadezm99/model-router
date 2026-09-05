"""FIFO queue with coalesce same-target and 202 Retry-After semantics."""

import asyncio
import time

QUEUE_TIMEOUT_S = 240
RETRY_AFTER_DEFAULT = 30


class GatewayQueue:
    """FIFO asyncio.Queue that coalesces same-target swaps.

    - enqueue returns Retry-After seconds
    - coalesce: concurrent same-target does not trigger extra systemctl start (counted)
    - qsize / depth reflects FIFO length
    - items expire after QUEUE_TIMEOUT_S -> caller should 504
    """

    def __init__(self, timeout_s: int = QUEUE_TIMEOUT_S):
        self._q: asyncio.Queue = asyncio.Queue()
        self.timeout_s = timeout_s
        self.swapping_target: str | None = None
        self.swap_started_at: float | None = None
        self.coalesced = 0
        self.swaps_total = 0

    def depth(self) -> int:
        return self._q.qsize()

    def is_swapping(self) -> bool:
        return self.swapping_target is not None

    def set_swapping(self, target: str) -> None:
        self.swapping_target = target
        self.swap_started_at = time.monotonic()
        self.swaps_total += 1

    def clear_swapping(self) -> None:
        self.swapping_target = None
        self.swap_started_at = None

    def _retry_after(self) -> int:
        if self.swap_started_at is None:
            return RETRY_AFTER_DEFAULT
        elapsed = time.monotonic() - self.swap_started_at
        remaining = max(1, int(self.timeout_s - elapsed))
        return min(remaining, RETRY_AFTER_DEFAULT)

    async def enqueue(self, target: str, request_id: str = "") -> dict:
        """Enqueue and return 202 payload. Coalesce if same target already swapping."""
        now = time.monotonic()
        if self.swapping_target is not None and target == self.swapping_target:
            self.coalesced += 1
        item = {"target": target, "request_id": request_id, "created_at": now}
        await self._q.put(item)
        return {"queued": True, "target": target, "retry_after": self._retry_after()}

    async def dequeue(self) -> dict | None:
        try:
            return self._q.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def drain_fifo(self) -> list[dict]:
        items = []
        while not self._q.empty():
            items.append(await self._q.get())
        return items

    def is_expired(self, item: dict) -> bool:
        return (time.monotonic() - item.get("created_at", 0)) >= self.timeout_s

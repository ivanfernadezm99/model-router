"""queue.depth() must not report placeholders that outlived QUEUE_TIMEOUT_S.

Nothing consumes GatewayQueue in production: each 202 leaves a dict in the FIFO
forever, so a frozen queue_depth made /health and the Prometheus gauge lie for
days (13 ghosts from a single swap on 2026-11-02).
"""

from types import SimpleNamespace

import pytest

import src.gateway.queue as queue_module
from src.gateway.queue import GatewayQueue


def _fake_clock(monkeypatch, start: float = 1000.0) -> dict:
    """Replace queue_module.time only, so asyncio keeps its real loop clock."""
    clock = {"now": start}
    monkeypatch.setattr(
        queue_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    return clock


@pytest.mark.asyncio
async def test_depth_drops_expired_placeholders(monkeypatch):
    clock = _fake_clock(monkeypatch)
    q = GatewayQueue(timeout_s=240)

    await q.enqueue("coder-14b-100k", "old-1")
    await q.enqueue("coder-14b-100k", "old-2")
    assert q.depth() == 2

    clock["now"] += 241
    assert q.depth() == 0
    # reading twice must stay idempotent (no underflow, no resurrection)
    assert q.depth() == 0


@pytest.mark.asyncio
async def test_depth_keeps_fresh_items_in_fifo_order(monkeypatch):
    clock = _fake_clock(monkeypatch)
    q = GatewayQueue(timeout_s=240)

    await q.enqueue("sdxl", "old")
    clock["now"] += 200
    await q.enqueue("sdxl", "fresh")
    clock["now"] += 41  # old = 241 s (expired), fresh = 41 s (alive)

    assert q.depth() == 1

    items = await q.drain_fifo()
    assert [item["request_id"] for item in items] == ["fresh"]

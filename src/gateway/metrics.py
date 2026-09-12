"""Metrics — /health with nvidia-smi + active_model, /metrics Prometheus text."""

from src.orchestrator.vram import get_vram_used_mb


def health_payload(orchestrator, queue) -> dict:
    vram = get_vram_used_mb()
    model = getattr(orchestrator, "active_model", None)
    depth = queue.depth() if queue else 0
    coalesced = getattr(queue, "coalesced", 0) if queue else 0
    swaps = getattr(queue, "swaps_total", 0) if queue else 0
    is_swapping = queue.is_swapping() if queue and hasattr(queue, "is_swapping") else False
    if model is None:
        status = "idle"
    elif is_swapping:
        status = "swapping"
    else:
        status = "ready"
    # vram_used_mb may be None -> null in json
    return {"model": model, "vram_used_mb": vram, "queue_depth": depth, "coalesced": coalesced, "swaps_total": swaps, "status": status}


def metrics_text(orchestrator, queue) -> str:
    vram = get_vram_used_mb()
    depth = queue.depth() if queue else 0
    swaps = getattr(queue, "swaps_total", 0) if queue else 0
    coalesced = getattr(queue, "coalesced", 0) if queue else 0
    vram_str = str(vram) if vram is not None else "0"
    lines = [
        "# HELP model_router_queue_depth current queue depth",
        "# TYPE model_router_queue_depth gauge",
        f"model_router_queue_depth {depth}",
        "# HELP model_router_swaps_total total swaps",
        "# TYPE model_router_swaps_total counter",
        f"model_router_swaps_total {swaps}",
        "# HELP model_router_coalesced_total total coalesced requests",
        "# TYPE model_router_coalesced_total counter",
        f"model_router_coalesced_total {coalesced}",
        "# HELP model_router_vram_used_mb VRAM used in MB",
        "# TYPE model_router_vram_used_mb gauge",
        f"model_router_vram_used_mb {vram_str}",
    ]
    active = getattr(orchestrator, "active_model", None) or ""
    lines.append(f'model_router_active_model{{model="{active}"}} 1')
    return "\n".join(lines) + "\n"

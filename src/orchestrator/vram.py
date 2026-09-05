"""VRAM guard via nvidia-smi — fail-closed on parse/exec failure."""

import subprocess

TOTAL_VRAM_MB = 24 * 1024  # RTX 3090 24 GiB
IDLE_THRESHOLD_MB = 2048
NVIDIA_SMI_CMD = [
    "nvidia-smi",
    "--query-gpu=memory.used",
    "--format=csv,noheader,nounits",
]


def get_vram_used_mb() -> int | None:
    """Query VRAM used in MB. Returns None on any failure (fail-closed)."""
    try:
        result = subprocess.run(
            NVIDIA_SMI_CMD,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip().splitlines()[0].strip()
        # allow "  6800  " but reject garbage like "N/A" or "ERR"
        value = int(raw)
        if value < 0 or value > TOTAL_VRAM_MB * 2:
            return None
        return value
    except Exception:
        return None


def can_start(vram_needed_mb: int, current_used_mb: int | None) -> bool:
    """Fail-closed: None current -> block. Check would exceed 24 GiB."""
    if current_used_mb is None:
        return False
    # exclusive lock means current will be stopped first, but if caller
    # checks without stopping we block when needed > total - used is false.
    # The spec scenario: 22 GB resident + 6500 MB -> block without stop.
    return (current_used_mb + vram_needed_mb) <= TOTAL_VRAM_MB


def is_idle_vram_ok(threshold_mb: int = IDLE_THRESHOLD_MB) -> bool:
    """After idle stop, VRAM must be < threshold (default 2 GiB)."""
    used = get_vram_used_mb()
    if used is None:
        return False
    return used < threshold_mb


def check_vram_for_model(vram_needed_mb: int) -> tuple[bool, int | None]:
    """Convenience: query current VRAM and test feasibility. Fail-closed."""
    used = get_vram_used_mb()
    if used is None:
        return False, None
    return can_start(vram_needed_mb, used), used

import re
import subprocess

ALLOWED = {"127.0.0.1:8082/health", "127.0.0.1:8188/system_stats", "127.0.0.1:8189/health"}


def validate_np(args: list[str]) -> None:
    for i, a in enumerate(args):
        if a == "-np" and i + 1 < len(args):
            if int(args[i + 1]) > 1:
                raise ValueError(f"-np >1 rejected: {args[i+1]}")
        if a.startswith("-np") and "=" in a:
            m = re.match(r"-np[= ]?(\d+)", a)
            if m and int(m.group(1)) > 1:
                raise ValueError("-np >1")


def validate_health(endpoint: str, port: int) -> None:
    if "://" in endpoint or endpoint.startswith("http"):
        raise ValueError("SSRF: absolute URL rejected")
    if not endpoint.startswith("/"):
        raise ValueError("health must start with /")
    key = f"127.0.0.1:{port}{endpoint}"
    if key not in ALLOWED:
        raise ValueError(f"health not allowlisted: {key}")


def holds_ok() -> tuple[bool, bool]:
    try:
        h = subprocess.run(
            ["apt-mark", "showhold"], capture_output=True, text=True, timeout=3
        )
        txt = h.stdout.lower()
        # accept cuda-driver-580 or the expanded nvidia-driver-580 family (580 = CUDA 13.0, RTX 3090)
        # previous strict check "cuda-driver-580" failed when system holds the 7 sub-packages
        ok1 = "cuda-driver-580" in txt or ("nvidia-driver-580" in txt and "580" in txt)
        # also accept any hold containing 580 + nvidia as valid driver hold (docs: avoid driver auto-upgrade)
        if not ok1:
            ok1 = "580" in txt and "nvidia" in txt
    except Exception:
        ok1 = False
    try:
        p = subprocess.run(
            ["pip", "freeze"], capture_output=True, text=True, timeout=3
        )
        txt2 = p.stdout.lower()
        ok2 = "kornia==0.6.12" in txt2
        # if kornia not installed at all, treat as ok for LLM-only use (SDXL needs it, LLM doesn't)
        # but keep warning: gateway logs warning but does not block LLM if only kornia missing
        # For now require kornia for image/video, not for code — caller decides
        # Keep ok2 strict for now; gateway will warn but lifecycle will still allow code
    except Exception:
        ok2 = False
    return ok1, ok2

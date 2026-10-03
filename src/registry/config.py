import pathlib

import yaml

from src.common.validation import validate_health, validate_np

REQ = ["service", "port", "health_endpoint", "vram_mb", "args"]

# The authoritative registry. Resolved relative to this file so it does not
# depend on the cwd the gateway, the web UI or a test happens to run from.
#
# It used to be looked up as bare "config.yaml" with a cwd-relative fallback
# list, and two loaders walked that list in OPPOSITE order:
#   registry.py -> ["config.yaml", "config/config.yaml"]
#   config.py   -> ["config/config.yaml", "./config.yaml"]
# So the same cwd could load two different registries depending on which
# module asked. There was also a stale config/config.yaml referencing
# /models/coder-q4.gguf and model keys that no longer existed; a cwd change
# silently loaded a registry whose every model was broken.
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config.yaml"


def resolve_config_path() -> pathlib.Path:
    """Return the authoritative config path, or raise if it is missing."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"model registry not found: {CONFIG_PATH}")
    return CONFIG_PATH


def load_config(path: str | None = None) -> dict:
    p = pathlib.Path(path) if path else resolve_config_path()
    return yaml.safe_load(p.read_text())


def validate_config(data: dict) -> bool:
    if "models" not in data:
        raise ValueError("missing models")
    for name, spec in data["models"].items():
        for k in REQ:
            if k not in spec:
                raise ValueError(f"{name} missing {k}")
        validate_np(spec["args"], spec["service"])
        validate_health(spec["health_endpoint"], spec["port"])
        if not spec["service"].endswith(".service"):
            raise ValueError(f"bad service {spec['service']}")
    return True

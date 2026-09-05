import pathlib
import yaml

from src.common.validation import validate_health, validate_np

REQ = ["service", "port", "health_endpoint", "vram_mb", "args"]


def load_config(path: str = "config.yaml") -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        for alt in ["config/config.yaml", "./config.yaml"]:
            if pathlib.Path(alt).exists():
                p = pathlib.Path(alt)
                break
    return yaml.safe_load(p.read_text())


def validate_config(data: dict) -> bool:
    if "models" not in data:
        raise ValueError("missing models")
    for name, spec in data["models"].items():
        for k in REQ:
            if k not in spec:
                raise ValueError(f"{name} missing {k}")
        validate_np(spec["args"])
        validate_health(spec["health_endpoint"], spec["port"])
        if not spec["service"].endswith(".service"):
            raise ValueError(f"bad service {spec['service']}")
    return True

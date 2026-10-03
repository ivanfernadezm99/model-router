import logging
import pathlib

import yaml

from src.common.validation import holds_ok
from src.registry.config import resolve_config_path, validate_config


class Registry:
    def __init__(self, path: str | None = None):
        self.path = path
        self.data: dict | None = None
        self.models: dict = {}

    def load(self) -> "Registry":
        p = pathlib.Path(self.path) if self.path else resolve_config_path()
        self.data = yaml.safe_load(p.read_text())
        validate_config(self.data)
        self.models = self.data["models"]
        return self

    def resolve(self, name: str) -> dict:
        name = (name or "").strip()
        if name in self.models:
            return self.models[name]
        # case-insensitive retry — clients vary the casing of model ids and a
        # KeyError here surfaces as a bare "unknown model" with no hint.
        lowered = {k.lower(): k for k in self.models}
        if name.lower() in lowered:
            logging.info("registry: resolved %r to %r case-insensitively", name, lowered[name.lower()])
            return self.models[lowered[name.lower()]]
        raise KeyError(f"unknown model: {name}")

    def check_holds(self) -> bool:
        a, b = holds_ok()
        if not (a and b):
            logging.warning(f"holds missing cuda={a} kornia={b}")
        return a and b


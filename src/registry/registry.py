import logging
import pathlib

import yaml

from src.common.validation import holds_ok
from src.registry.config import validate_config


class Registry:
    def __init__(self, path: str = "config.yaml"):
        self.path = path
        self.data: dict | None = None
        self.models: dict = {}

    def load(self) -> "Registry":
        p = pathlib.Path(self.path)
        if not p.exists():
            for alt in ["config.yaml", "config/config.yaml"]:
                if pathlib.Path(alt).exists():
                    p = pathlib.Path(alt)
                    break
        self.data = yaml.safe_load(p.read_text())
        validate_config(self.data)
        self.models = self.data["models"]
        return self

    def resolve(self, name: str) -> dict:
        if name not in self.models:
            raise KeyError(f"unknown model: {name}")
        return self.models[name]

    def check_holds(self) -> bool:
        a, b = holds_ok()
        if not (a and b):
            logging.warning(f"holds missing cuda={a} kornia={b}")
        return a and b

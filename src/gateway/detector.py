"""Task detector — X-Model-Hint header > body prefix case-insensitive, default code, valida contra registry."""

import json
import logging

logger = logging.getLogger(__name__)

ALLOWED = {"code", "image", "video"}
# task -> registry model key (code uses defaults.model)
TASK_TO_MODEL = {
    "code": "coder-14b-200k",
    "image": "sdxl",
    "video": "sdxl",
}

PREFIXES = ("code:", "image:", "video:")


def _body_text(body: bytes | dict | str | None) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if isinstance(body, dict):
        # try common OpenAI fields: prompt, messages[0].content, input
        for key in ("prompt", "input"):
            v = body.get(key)
            if isinstance(v, str):
                return v
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0]
        msgs = body.get("messages")
        if isinstance(msgs, list) and msgs:
            first = msgs[0]
            if isinstance(first, dict):
                c = first.get("content")
                if isinstance(c, str):
                    return c
        # fallback dump truncated
        try:
            return json.dumps(body)[:512]
        except Exception:
            return str(body)[:512]
    if isinstance(body, str):
        return body
    return str(body)[:512]


def detect_task(headers: dict, body: bytes | dict | str | None, registry=None) -> str:
    """Return code|image|video. Header wins, then prefix, then default code.
    Validates header/prefix against ALLOWED and validates target model in registry.
    Raises ValueError on unknown hint (caller should 400).
    """
    hint = None
    # case-insensitive header lookup
    for k, v in (headers or {}).items():
        if k.lower() == "x-model-hint":
            hint = str(v).strip()
            break
    if hint is not None:
        low = hint.lower()
        if low not in ALLOWED:
            raise ValueError(f"unknown X-Model-Hint: {hint}")
        task = low
        _validate_registry(task, registry)
        return task

    text = _body_text(body).lstrip()
    low_text = text.lower()
    for p in PREFIXES:
        if low_text.startswith(p):
            task = p[:-1]  # strip colon
            _validate_registry(task, registry)
            return task

    # default fallback
    logger.info(json.dumps({"routing": "default:code", "hint": hint, "body_prefix": text[:32]}))
    _validate_registry("code", registry)
    return "code"


def _validate_registry(task: str, registry) -> None:
    if registry is None:
        return
    model_key = TASK_TO_MODEL.get(task)
    if model_key is None:
        raise ValueError(f"no model mapping for task {task}")
    # registry may have different keys (e.g. sdxl) — validate present
    try:
        # .models is dict after load()
        models = getattr(registry, "models", None) or {}
        if models and model_key not in models:
            # allow code fallback to any coder* if exact missing
            if task == "code" and any(k.startswith("coder") for k in models):
                return
            raise KeyError(model_key)
    except KeyError as exc:
        raise ValueError(f"unknown model for task {task}: {exc}") from exc


def task_to_model(task: str) -> str:
    return TASK_TO_MODEL[task]

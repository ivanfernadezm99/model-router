"""Centralized error notifier — Telegram + persistent file log."""
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger("model_router.errors")

# persistent file — always append, never rotate here
LOG_FILE = Path(__file__).resolve().parents[2] / "logs" / "router-errors.log"

# simple cooldown to avoid spamming Telegram on repeated client cancels
_last_sent: dict[str, float] = {}
_COOLDOWN_S = 300  # 5 min per title


def _send_telegram(title: str, detail: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    # cooldown per title — file log always, Telegram throttled
    try:
        import time
        now = time.monotonic()
        last = _last_sent.get(title, 0)
        if now - last < _COOLDOWN_S:
            return
        _last_sent[title] = now
    except Exception:
        pass
    try:
        text = f"⚠️ {title}\n{detail[:700]}"
        if len(detail) > 700:
            text += "\n…(truncado)"
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)


def _append_file(title: str, detail: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} | {title} | {detail[:800].replace(chr(10), ' ')}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("file log append failed: %s", exc)


def notify_error(title: str, detail: str, level: str = "error") -> None:
    """Fire-and-forget: logger + file + Telegram. Never raises."""
    clean = detail[:800].replace("\n", " ")
    if level == "warning":
        logger.warning("%s — %s", title, clean)
    else:
        logger.error("%s — %s", title, clean)
    _append_file(title, detail)
    _send_telegram(title, detail)


def notify_warning(title: str, detail: str) -> None:
    notify_error(title, detail, level="warning")

"""Çalışma zamanı yardımcıları: günlükleme ve ayarların etkin değerleri."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from sqlalchemy.orm import Session

from .config import Settings

_configured = False

KEY_HEARTBEAT_ALERT = "last_heartbeat_alert"
KEY_LLM_CALLS_PREFIX = "llm_calls:"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def setup_logging(settings: Settings) -> None:
    global _configured
    if _configured:
        return
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    file_handler = RotatingFileHandler(settings.log_dir / "enerji_takip.log", maxBytes=2_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):  # ör. MCP SDK zaten stderr'e ekliyorsa
        root.addHandler(stream)
    root.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


def validate_time(value: str) -> str:
    value = value.strip()
    if not _TIME_RE.match(value):
        raise ValueError("Saat SS:DD biçiminde olmalı (ör. 08:00)")
    return value


def effective_recipients(session: Session | None, settings: Settings) -> tuple[list[str], list[str]]:
    return list(settings.email_to), list(settings.email_cc)


def effective_report_time(session: Session | None, settings: Settings) -> str:
    return settings.daily_report_time

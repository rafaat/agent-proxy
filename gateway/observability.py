"""Structured logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api_key=)[^&\s\"']+"),
    re.compile(r"(?i)(session_id=)[^&\s\"']+"),
    re.compile(r"()\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
]

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {
    "message",
    "asctime",
    "color_message",
}


class Redactor:
    """Masks known secret patterns and explicitly registered secret values."""

    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(r"\1***", text)
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return self._redact(json.dumps(payload, default=str))


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(Redactor(secrets)))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn installs its own handlers; route everything through ours so
    # access logs (which include ?api_key=...) are redacted too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True

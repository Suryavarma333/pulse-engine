from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_RESERVED_LOG_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__)
_SENSITIVE_KEY_PARTS = ("password", "secret", "token", "credential")


def _safe_log_value(key: str, value: object) -> object:
    if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_log_value(str(child_key), child)
            for child_key, child in value.items()
        }
    if isinstance(value, list | tuple):
        return [_safe_log_value(key, child) for child in value]
    return value


class JsonFormatter(logging.Formatter):
    """Small JSON formatter that keeps structured fields explicit and serializable."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_KEYS and key != "message":
                payload[key] = _safe_log_value(key, value)
        return json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    logger.log(level, event, extra=dict(fields))


def structured_fields(values: Mapping[str, object]) -> dict[str, object]:
    """Return a detached mapping suitable for a logging ``extra`` payload."""

    return dict(values)

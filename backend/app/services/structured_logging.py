from __future__ import annotations

import json
import logging
import re
import sys
import time
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from app.core.config import Settings

correlation_id_context: ContextVar[str | None] = ContextVar(
    "dialecticore_correlation_id",
    default=None,
)

CORRELATION_RESPONSE_HEADER = "x-correlation-id"
CORRELATION_REQUEST_HEADERS = ("x-correlation-id", "x-request-id")
_SAFE_CORRELATION_ID = re.compile(r"[^A-Za-z0-9_.:-]")
_PATH_ID_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "episode_id": (re.compile(r"/episodes/([^/]+)"),),
    "asset_id": (re.compile(r"/assets/([^/]+)"),),
    "approval_id": (re.compile(r"/approvals/([^/]+)"),),
    "turn_id": (re.compile(r"/discussion/turns/([^/]+)"),),
    "job_id": (re.compile(r"/jobs/([^/]+)"),),
    "workflow_id": (
        re.compile(r"/comfyui-workflows/([^/]+)"),
        re.compile(r"/workflows/([^/]+)"),
    ),
}
_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "schema_version": "dialecticore.log_event.v1",
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            "timestamp_ms": int(record.created * 1000),
        }
        correlation_id = getattr(record, "correlation_id", None) or current_correlation_id()
        if correlation_id:
            payload["correlation_id"] = correlation_id
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            payload.update(structured)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def setup_structured_logging(settings: Settings) -> None:
    logger = logging.getLogger("dialecticore")
    logger.setLevel(_normalized_log_level(settings.log_level))
    logger.propagate = False
    if any(getattr(handler, "_dialecticore_structured", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())
    handler._dialecticore_structured = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def _normalized_log_level(value: str) -> int:
    level_name = value.strip().upper()
    if level_name not in _LOG_LEVELS:
        allowed = ", ".join(sorted(_LOG_LEVELS))
        raise RuntimeError(
            "DIALECTICORE_LOG_LEVEL must be one of "
            f"{allowed}; got {value!r}"
        )
    return _LOG_LEVELS[level_name]


def request_log_payload(
    *,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_host: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "dialecticore.api_request_log.v1",
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 3),
    }
    if client_host:
        payload["client_host"] = client_host
    payload.update(path_identifiers(path))
    return payload


def path_identifiers(path: str) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for key, patterns in _PATH_ID_PATTERNS.items():
        for pattern in patterns:
            match = pattern.search(path)
            if match:
                identifiers[key] = match.group(1)
                break
    return identifiers


def correlation_id_from_headers(headers: Any) -> str:
    for header_name in CORRELATION_REQUEST_HEADERS:
        raw = headers.get(header_name)
        if raw:
            return sanitize_correlation_id(raw)
    return f"req-{uuid4()}"


def sanitize_correlation_id(value: str) -> str:
    cleaned = _SAFE_CORRELATION_ID.sub("-", value.strip())[:128]
    return cleaned or f"req-{uuid4()}"


def current_correlation_id() -> str | None:
    return correlation_id_context.get()


def monotonic_ms() -> float:
    return time.perf_counter() * 1000

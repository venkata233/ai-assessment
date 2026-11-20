"""Structured JSON logging with a redaction safety net (A09: no keys, raw IPs, or full user agents)."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "x-api-key",
        "x_api_key",
        "authorization",
        "password",
        "secret",
        "token",
        "client_ip",
        "ip",
        "remote_addr",
        "user_agent",
        "user-agent",
        "x-forwarded-for",
        "database_url",
        "redis_url",
    }
)
REDACTED = "[redacted]"


def redact_sensitive(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_sensitive,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )

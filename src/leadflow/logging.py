"""Structured logging with secret redaction built into the pipeline.

Redaction is a processor, not a convention. The way a key usually escapes is
inside the repr of a request in an error message, not in a line someone wrote on
purpose, so the processor scans values as well as key names.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

_SECRET_KEY = re.compile(r"(key|secret|token|password|authorization)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(sk-ant-[A-Za-z0-9\-_]{8,}|pat-[A-Za-z0-9\-]{8,}|Bearer\s+\S{12,})")
REDACTED = "[REDACTED]"


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: REDACTED if _SECRET_KEY.search(str(k)) else _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    redacted: EventDict = _redact(dict(event_dict))
    return redacted


def configure_logging(*, json: bool = False, level: int = logging.INFO) -> None:
    """JSON lines when unattended, readable console output when a person is watching."""
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

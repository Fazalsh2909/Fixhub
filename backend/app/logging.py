"""Structured JSON logging with secret redaction. Never log keys/tokens/CoT."""

import json
import logging
import re
import sys

_REDACT = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization).{0,20}[:=]\s*\S+"
)


def _redact(obj: object) -> object:
    if isinstance(obj, dict):
        out: dict[object, object] = {}
        for k, v in obj.items():
            if re.search(r"(?i)(key|token|secret|password|auth)", str(k)):
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, str):
        return _REDACT.sub("***REDACTED***", obj)
    return obj


class JsonHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            extra = _redact(getattr(record, "extra_fields"))
            if isinstance(extra, dict):
                payload.update(extra)
        sys.stdout.write(json.dumps(_redact(payload), default=str) + "\n")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(JsonHandler())
        logger.setLevel(logging.INFO)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    rec = logging.LogRecord(
        name=logger.name,
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=event,
        args=(),
        exc_info=None,
    )
    rec.extra_fields = fields  # type: ignore[attr-defined]
    logger.handle(rec)

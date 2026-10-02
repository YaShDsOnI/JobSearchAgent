import json
import logging
import sys
from datetime import UTC, datetime

from app.core.redaction import redact


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        error_type = getattr(record, "error_type", None)
        if error_type:
            payload["error_type"] = error_type
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # HTTP client libraries log full request URLs (paths and query strings) at INFO/DEBUG.
    # Outbound activity is recorded by the source audit trail without paths or queries, so
    # these loggers are capped at WARNING regardless of the application log level.
    for name in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


QUIET_LIBRARY_LOGGERS = ("httpx", "httpcore")

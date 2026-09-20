"""JSON structured logging. Every line automatically carries the current request_id."""
import json
import logging
import sys
from datetime import datetime, timezone

from app.request_context import current_request_id


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or current_request_id()
        if rid:
            payload["request_id"] = rid
        for key in ("event", "entity_id", "inquiry_id"):
            if hasattr(record, key):
                payload[key] = str(getattr(record, key))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

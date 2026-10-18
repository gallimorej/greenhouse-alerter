"""JSON logs on stdout, in the shape Cloud Logging parses (``severity`` and ``message``).

Structured fields go in ``extra={"json": {...}}`` and land at the top level of ``jsonPayload``, which
is what the log-based metrics in terraform/monitoring.tf filter on.
"""

import json
import logging
import sys
from datetime import date, datetime


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        entry.update(getattr(record, "json", None) or {})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=_default)


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # urllib3 logs request paths at DEBUG, and the IFTTT key is in the path.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

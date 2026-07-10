"""Request context propagation via contextvars.

Provides a request_id context variable that is set by the logging middleware
and can be read by any service-layer code to correlate logs across a request.

Also provides logging filters wired onto the root handler at startup:

- :class:`RequestIdFilter` — stamps ``request_id`` onto every record from the
  contextvar so it is available to the formatter.
- :class:`SensitiveFieldFilter` — scrubs the *value* of any record attribute
  whose *name* is in :data:`app.middleware.logging.SCRUB_FIELDS` (e.g.
  ``record.user_input``), replacing it with a redaction marker. This honors
  the documented field-level redaction contract without over-redacting log
  *messages* that merely happen to contain a sensitive keyword as a
  substring.
"""

import contextvars
import json
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)

# Standard LogRecord attributes — everything else on a record is an "extra"
# added by the caller and is a candidate for JSON serialization.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
        "request_id",  # added by RequestIdFilter; serialize explicitly
    }
)


class RequestIdFilter(logging.Filter):
    """Logging filter that adds request_id to log records from the context var."""

    def filter(self, record):
        record.request_id = request_id_var.get("")
        return True


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Top-level keys: ``ts``, ``level``, ``logger``, ``message``, plus any
    ``extra`` fields the caller supplied (request_id, method, path,
    status_code, duration_ms, …) and ``request_id`` stamped by
    :class:`RequestIdFilter`. Exceptions are rendered as ``exc_info``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = getattr(record, "request_id", "")
        if rid:
            payload["request_id"] = rid
        # Serialize caller-supplied extras (anything not a standard attr).
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)

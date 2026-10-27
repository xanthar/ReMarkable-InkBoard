"""structlog setup: human-readable console output on stderr.

Handwriting is personal, so no coordinate or pressure field, and no screen frame, ever reaches
a log line: the ``drop_handwriting`` processor removes those keys whatever a call site passes.
Tracebacks are rendered plain, without local variables, so a frame held in a local never
reaches the console either.
"""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

HANDWRITING_KEYS = frozenset(
    {"x", "y", "u", "v", "p", "pressure", "pts", "point", "points", "frame", "rgba", "bgra", "raw"}
)


def drop_handwriting(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in HANDWRITING_KEYS:
        event_dict.pop(key, None)
    return event_dict


class StderrLogger:
    """Writes rendered lines to whatever ``sys.stderr`` is at call time (never a stale handle)."""

    def msg(self, message: str) -> None:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()

    log = debug = info = warning = warn = error = critical = exception = fatal = msg


def _stderr_logger(*_args: Any) -> StderrLogger:
    return StderrLogger()


def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            drop_handwriting,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(colors=False, exception_formatter=structlog.dev.plain_traceback),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )

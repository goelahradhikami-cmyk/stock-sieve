"""Project-level logging configuration for stock-sieve.

Provides a ``get_logger(name)`` helper that returns a configured logger.
The root project logger is configured once (idempotent); subsequent calls
only attach a per-module handler if missing, so repeated invocations never
stack duplicate handlers.

Level can be tuned via the ``LOG_LEVEL`` environment variable
(e.g. ``LOG_LEVEL=DEBUG``). Default level is ``INFO``.
"""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s | %(name)s | %(levelname)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_PROJECT_LOGGER_NAME = "stock_sieve"

_configured = False


def _configure_project_logger() -> logging.Logger:
    """Configure (once) the project root logger with a console handler."""
    global _configured
    logger = logging.getLogger(_PROJECT_LOGGER_NAME)

    if _configured:
        return logger

    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    # Avoid propagating to the root logger to prevent duplicate output when
    # the root logger is also configured by a third party (e.g. Streamlit).
    logger.propagate = False

    # Attach a single console handler.
    has_stream_handler = any(
        isinstance(h, logging.StreamHandler) for h in logger.handlers
    )
    if not has_stream_handler:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        logger.addHandler(handler)

    _configured = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a project logger for the given module name.

    The project root logger is configured exactly once. Child loggers
    obtained via this function inherit handlers from the root project logger,
    so calling ``get_logger`` repeatedly never stacks duplicate handlers.
    """
    _configure_project_logger()

    if not name:
        return logging.getLogger(_PROJECT_LOGGER_NAME)

    # Child logger under the project namespace so it inherits handlers.
    if not name.startswith(_PROJECT_LOGGER_NAME):
        child_name = f"{_PROJECT_LOGGER_NAME}.{name}"
    else:
        child_name = name

    child = logging.getLogger(child_name)
    # Ensure child respects the project level unless explicitly overridden.
    if child.level == logging.NOTSET:
        child.setLevel(logging.getLogger(_PROJECT_LOGGER_NAME).level)
    return child


# Configure on import so that simply importing the module sets up logging.
_configure_project_logger()

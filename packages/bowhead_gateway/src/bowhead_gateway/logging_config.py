"""Process logging setup shared by the gateway and its operator commands."""

from __future__ import annotations

import logging

import structlog


def structlog_processors(log_format: str) -> list[structlog.types.Processor]:
    """Return the log processor chain; exception logs carry a plain traceback without local variables.

    structlog's default console formatter renders every frame's locals when rich is installed. Locals hold
    provider adapters with their API keys, prompts and user data, and rendering them runs on the event loop:
    on 116 (2026-11-08) one exception printed about 110,000 lines and stalled the gateway for about 40 seconds.
    """
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    if log_format == "json":
        # Without this the JSON line keeps only `exc_info: true` and the traceback is lost.
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True, exception_formatter=structlog.dev.plain_traceback))
    return processors


def configure_logging(log_level: str, log_format: str) -> None:
    """Configure standard-library logging and structlog for this process."""

    logging.basicConfig(level=log_level.upper())
    structlog.configure(
        processors=structlog_processors(log_format),
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level.upper())),
        cache_logger_on_first_use=True,
    )

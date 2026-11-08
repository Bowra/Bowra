"""Exception logs keep their traceback but never print local variables, in either log format."""

from __future__ import annotations

import io
import json

import pytest
import structlog
from bowhead_gateway.logging_config import structlog_processors
from structlog.dev import ConsoleRenderer, plain_traceback

SECRET = "sk-local-variable-must-not-be-logged"


def _fail_with_secret_local() -> None:
    provider_api_key = SECRET  # noqa: F841 - a frame local that a locals-rendering formatter would print
    raise RuntimeError("provider stream interrupted")


@pytest.fixture
def production_console_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore structlog's production console default, which conftest swaps for plain tracebacks in tests."""
    rich_default = structlog.dev.default_exception_formatter
    assert rich_default is not plain_traceback, "rich must be installed for this regression check to mean anything"
    defaults = ConsoleRenderer.__init__.__defaults__ or ()
    monkeypatch.setattr(
        ConsoleRenderer.__init__,
        "__defaults__",
        tuple(rich_default if value is plain_traceback else value for value in defaults),
    )


@pytest.mark.parametrize("log_format", ["pretty", "json"])
@pytest.mark.usefixtures("production_console_defaults")
def test_exception_logs_keep_traceback_without_locals(log_format: str) -> None:
    previous = structlog.get_config()
    out = io.StringIO()
    structlog.configure(
        processors=structlog_processors(log_format),
        logger_factory=structlog.PrintLoggerFactory(out),
        cache_logger_on_first_use=False,
    )
    try:
        try:
            _fail_with_secret_local()
        except RuntimeError:
            structlog.get_logger().exception("provider.call_failed")
    finally:
        structlog.configure(**previous)

    text = out.getvalue()
    assert SECRET not in text
    assert "RuntimeError: provider stream interrupted" in text
    assert "_fail_with_secret_local" in text
    if log_format == "json":
        assert "provider stream interrupted" in json.loads(text)["exception"]

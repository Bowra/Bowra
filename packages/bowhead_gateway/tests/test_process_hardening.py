"""The gateway marks itself non-dumpable on Linux (visual self-check D5 hardening)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from bowhead_gateway import process_hardening

pytestmark = pytest.mark.unit


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def test_not_linux_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    log = _Log()
    assert process_hardening.deny_same_user_inspection(log) is False and log.events == []


def test_linux_sets_and_confirms_the_flag(monkeypatch) -> None:
    import ctypes

    calls: list[tuple[int, ...]] = []

    class _Libc:
        def prctl(self, *args: int) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: _Libc())
    log = _Log()
    assert process_hardening.deny_same_user_inspection(log) is True
    assert calls == [(process_hardening.PR_SET_DUMPABLE, 0, 0, 0, 0), (process_hardening.PR_GET_DUMPABLE, 0, 0, 0, 0)]
    assert log.events == [("gateway.process_hardening", {"dumpable": 0})]


def test_a_failure_is_logged_and_never_raises(monkeypatch) -> None:
    import ctypes

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("no libc")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", broken)
    log = _Log()
    assert process_hardening.deny_same_user_inspection(log) is False
    assert log.events == [("gateway.process_hardening_failed", {"error_type": "OSError"})]

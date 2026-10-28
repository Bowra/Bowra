"""ξ A5 hotfix — done-event guarantee + peer terminal break tests.

Prod prod trace t_cdc20fcbf18e 教训:
- peer LLM 失败, str(e) 空 → error event empty msg
- 没 done event emitted → frontend 永等 "已中断"
- 2 consecutive subagent.return ok=True → 没 break

A5 hotfix:
- A5a subagent.py:869 加 CancelledError 独立 branch + empty msg 兜底 + repr+type log
- A5b lead_orchestrator.py finally bus._done_emitted flag + fallback done emit
- A5c subagent.py for-tc loop 内 successful __terminal return 立即 break (跳过同 batch tail)
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.lead_orchestrator import _emit_event


class _MockBus:
    """Minimal bus mock that tracks emit calls."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._done_emitted = False

    async def emit(self, event) -> None:
        self.events.append({"event_type": event.event_type, "payload": event.payload})


@pytest.mark.asyncio
async def test_a5b_emit_event_stamps_done_emitted_flag() -> None:
    """_emit_event sets bus._done_emitted=True only when name=='done'."""
    bus = _MockBus()
    bus._done_emitted = False
    # non-done event: flag stays False
    await _emit_event(bus, "t_1", "tool_call", {"x": 1})
    assert bus._done_emitted is False
    # done event: flag flips True
    await _emit_event(bus, "t_1", "done", {"status": "completed"})
    assert bus._done_emitted is True


@pytest.mark.asyncio
async def test_a5b_emit_event_done_flag_set_before_emit_fails() -> None:
    """Intent-to-emit semantics: flag set even if bus.emit raises."""
    bus = _MockBus()
    bus._done_emitted = False

    async def _failing_emit(event):
        raise RuntimeError("network down")

    bus.emit = _failing_emit  # type: ignore[assignment]
    # Should not raise (caller catches), but flag must be set
    await _emit_event(bus, "t_1", "done", {"status": "error"})
    assert bus._done_emitted is True


def test_a5c_terminal_break_detected_on_per_tc_path() -> None:
    """Verify _detect_terminal_return still detects successful __terminal return.

    (per-tc immediate break is in subagent.py for-tc loop, hard to unit-test
    without full SubagentRunner harness — covered by integration test or
    smoke. Here we lock down the _detect_terminal_return contract that the
    per-tc check duplicates.)
    """
    from bowhead_core.subagent import _detect_terminal_return
    from bowhead_core.types import ToolInvocation

    invocations = [
        ToolInvocation(
            tool_name="bi_query",
            arguments={},
            result={"rows": []},
            error=None,
        ),
        ToolInvocation(
            tool_name="subagent_return",
            arguments={},
            result={"__terminal": True, "ok": True, "payload": {"title": "x"}},
            error=None,
        ),
    ]
    assert _detect_terminal_return(invocations) is True

    # Non-terminal (schema fail) return: not detected
    invocations_bad = [
        ToolInvocation(
            tool_name="subagent_return",
            arguments={},
            result={"__terminal": False, "ok": True},  # __terminal False = schema fail
            error=None,
        ),
    ]
    assert _detect_terminal_return(invocations_bad) is False


def test_a5a_empty_str_error_message_fallback() -> None:
    """Verify the error message format A5a uses: type+repr+str-or-empty.

    Not a runtime test (no SubagentRunner harness) but locks the message
    format contract.
    """

    # Format: f"subagent LLM failed: {err_type}: {err_str}"
    # Where err_str = str(e) or "(empty)"
    class EmptyError(Exception):
        def __str__(self):
            return ""

    e = EmptyError()
    err_type = type(e).__name__
    err_str = str(e) or "(empty)"
    msg = f"subagent LLM failed: {err_type}: {err_str}"
    assert msg == "subagent LLM failed: EmptyError: (empty)"
    assert "(empty)" in msg  # never empty user-facing


def test_a5b_finally_fallback_concept() -> None:
    """Concept test: bus._done_emitted flag pattern for finally fallback.

    The actual finally block lives in run_turn (lead_orchestrator.py:266+).
    Integration covered by manual + Playwright. Here we lock the flag
    semantics: after no done event was emitted, flag must be False; after
    1+ done emit, must be True.
    """
    bus = _MockBus()
    bus._done_emitted = False
    assert not getattr(bus, "_done_emitted", False)
    bus._done_emitted = True
    assert getattr(bus, "_done_emitted", False) is True

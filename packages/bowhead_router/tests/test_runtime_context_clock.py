"""The Router's <runtime_context> banner states the model clock's date, not a separate UTC "today".

Every LLM call gets this banner; the subagent ``<current_date>`` anchor and BI
advice read the same ``model_clock``. 2026-09-30T20:00Z is September 30 in Los
Angeles (and at UTC) but October 1 in Shanghai, so the old UTC banner and the
+8 anchor disagreed on the date, month and quarter in the same turn.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from bowhead_router.router import _inject_runtime_context, _today_banner
from bowhead_router.types import LLMRequest, Message

from bowhead_router import model_clock

pytestmark = pytest.mark.unit

_EDGE = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return _EDGE.astimezone(tz) if tz is not None else _EDGE.replace(tzinfo=None)

    monkeypatch.setattr(model_clock, "datetime", _Frozen)


def test_banner_uses_the_turn_viewer_zone() -> None:
    with model_clock.model_clock_zone(ZoneInfo("America/Los_Angeles")):
        banner = _today_banner()
    assert "today: 2026-09-30 (周三)" in banner
    assert "this_month: 2026-09" in banner
    assert "this_quarter: Q3 of 2026" in banner

    with model_clock.model_clock_zone(ZoneInfo("Asia/Shanghai")):
        banner = _today_banner()
    assert "today: 2026-10-01 (周四)" in banner
    assert "this_quarter: Q4 of 2026" in banner


def test_banner_without_a_viewer_zone_uses_the_deploy_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_TZ_OFFSET_HOURS", raising=False)  # default +8
    assert "today: 2026-10-01" in _today_banner()
    monkeypatch.setenv("BOWHEAD_TZ_OFFSET_HOURS", "0")
    assert "today: 2026-09-30" in _today_banner()


def test_injected_system_prompt_carries_the_viewer_date() -> None:
    req = LLMRequest(model="m", messages=[Message(role="user", content="去年销量")], system="base")
    with model_clock.model_clock_zone(ZoneInfo("America/Los_Angeles")):
        system = _inject_runtime_context(req).system
    assert system.startswith("<runtime_context>\ntoday: 2026-09-30")
    assert system.endswith("\n\nbase")

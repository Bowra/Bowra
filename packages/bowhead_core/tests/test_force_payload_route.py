"""M137 #145 — feature flag + 路由判断 + guard 测试."""

from __future__ import annotations

import pytest
from bowhead_core.canvas_composer import (
    _canary_pick,
    _decide_by_intent,
    _read_force_payload_flag,
    compose_payload_missing_guard,
    payload_required_for,
)
from bowhead_core.types import IntentType, ToolInvocation, TurnRequest

# ─────────────────────────────────────────────────────────────
# flag reading
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_flag_on_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # WP6 cleanup: always on by default (align dev=prod; legacy paths no longer survive silently in dev).
    monkeypatch.delenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", raising=False)
    monkeypatch.delenv("BOWHEAD_ENV", raising=False)
    assert _read_force_payload_flag() == "on"


@pytest.mark.unit
def test_flag_on_when_prod_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", raising=False)
    monkeypatch.setenv("BOWHEAD_ENV", "prod")
    assert _read_force_payload_flag() == "on"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["off", "canary", "on"])
def test_flag_explicit_overrides(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", value)
    assert _read_force_payload_flag() == value


@pytest.mark.unit
def test_flag_invalid_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "garbage")
    monkeypatch.delenv("BOWHEAD_ENV", raising=False)
    # Unrecognized value → default 'on' (WP6).
    assert _read_force_payload_flag() == "on"


# ─────────────────────────────────────────────────────────────
# decision matrix
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "intent,complexity,expected",
    [
        (IntentType.REPORT, 0.1, True),
        (IntentType.REPORT, 0.9, True),
        (IntentType.CAUSAL, 0.5, True),
        # UX-3 (2026-04-29): COMPUTE threshold 0.7 → 0.5; COMPARISON ≥ 0.5; TREND ≥ 0.7.
        (IntentType.COMPUTE, 0.7, True),
        (IntentType.COMPUTE, 0.5, True),  # New threshold boundary (≥ 0.5)
        (IntentType.COMPUTE, 0.49, False),
        (IntentType.COMPUTE, 0.0, False),
        (IntentType.LOOKUP, 0.9, False),
        (IntentType.TREND, 0.9, True),  # Triggers at ≥ 0.7.
        (IntentType.TREND, 0.7, True),
        (IntentType.TREND, 0.69, False),
        (IntentType.COMPARISON, 0.9, True),  # Triggers at ≥ 0.5.
        (IntentType.COMPARISON, 0.5, True),
        (IntentType.COMPARISON, 0.49, False),
        (IntentType.MODELING, 0.9, False),
        (IntentType.AMBIGUOUS, 0.5, False),
    ],
)
def test_decide_by_intent(intent: IntentType, complexity: float, expected: bool) -> None:
    assert _decide_by_intent(intent, complexity) is expected


# ─────────────────────────────────────────────────────────────
# canary distribution (~50%)
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_canary_pick_roughly_50_percent() -> None:
    n = 1000
    picks = [_canary_pick(f"t_{i}") for i in range(n)]
    rate = sum(picks) / n
    assert 0.4 < rate < 0.6


@pytest.mark.unit
def test_canary_pick_stable_per_turn() -> None:
    """同 turn_id 多次调返同结果（hash 稳定）."""
    tid = "t_test_xyz"
    assert _canary_pick(tid) == _canary_pick(tid)


# ─────────────────────────────────────────────────────────────
# payload_required_for end-to-end
# ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_required_off_always_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "off")
    assert not payload_required_for(IntentType.REPORT, 0.9, "t_x")
    assert not payload_required_for(IntentType.CAUSAL, 0.9, "t_x")


@pytest.mark.unit
def test_required_on_report_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "on")
    assert payload_required_for(IntentType.REPORT, 0.5, "t_x")


@pytest.mark.unit
def test_required_on_lookup_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "on")
    assert not payload_required_for(IntentType.LOOKUP, 0.9, "t_x")


@pytest.mark.unit
def test_required_canary_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "canary")
    n = 1000
    rate = sum(payload_required_for(IntentType.REPORT, 0.5, f"t_{i}") for i in range(n)) / n
    assert 0.4 < rate < 0.6


@pytest.mark.unit
def test_required_canary_lookup_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """canary 模式下 lookup 应该 0 触发（先 intent 判定 False）."""
    monkeypatch.setenv("BOWHEAD_M137_FORCE_PAYLOAD_ROUTE", "canary")
    assert not payload_required_for(IntentType.LOOKUP, 0.9, "t_anything")


# ─────────────────────────────────────────────────────────────
# guard composer
# ─────────────────────────────────────────────────────────────


class _StubBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, ev) -> None:
        self.events.append((ev.event_type, ev.payload))


class _StubSection:
    def __init__(self, sid: str, seq: int, kind: str, payload: dict) -> None:
        self.id = sid
        self.seq = seq
        self.kind = kind
        self.payload = payload

    def to_report_section(self) -> dict:
        return {"id": self.id, "kind": self.kind, **self.payload}


class _StubCanvas:
    def __init__(self) -> None:
        self.id = "cv_guard"
        self.session_id = "s_test"
        self.title = None


class _StubCanvasStore:
    def __init__(self) -> None:
        self._appended: list[_StubSection] = []
        self._seq = 0

    async def ensure_canvas(self, session_id: str, workspace_id: str, user_id: str):
        return _StubCanvas()

    async def append_section(self, *, canvas_id: str, kind: str, payload: dict, by_turn_id: str, owner: str | None = None):
        self._seq += 1
        sid = f"sec_{self._seq:03d}"
        sec = _StubSection(sid, self._seq, kind, payload)
        self._appended.append(sec)
        return sec


@pytest.mark.unit
async def test_guard_emits_3_sections_and_no_kpi() -> None:
    store = _StubCanvasStore()
    bus = _StubBus()
    request = TurnRequest(
        turn_id="t_guard",
        session_id="s_g",
        workspace_id="ws_g",
        user_id="u_g",
        member_uid="m_g",
        text="测试 guard",
    )
    invs = [
        ToolInvocation(tool_name="bi_fetch_rows", arguments={}, result={}),
        ToolInvocation(tool_name="add_chart", arguments={}, result={}),
    ]
    kinds = await compose_payload_missing_guard(
        canvas_store=store,
        request=request,
        bus=bus,
        intent=IntentType.REPORT,
        invocations=invs,
    )
    # Prod fix #79/#80: guard now has 2 sections (eyebrow + narrative), with no technical h1.
    assert kinds == ["eyebrow", "narrative"]
    # No KPI / insight_card / recommendations.
    assert "kpi_row" not in kinds
    assert "insight_card" not in kinds
    assert "recommendations" not in kinds
    # Bus contains a payload_missing_guard event.
    types = [t for t, _ in bus.events]
    assert "payload_missing_guard" in types
    # canvas.done payload source = "guard"
    done_payload = bus.events[-1][1]
    assert done_payload["source"] == "guard"

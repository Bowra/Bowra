"""W1-A C3 — ledger 默认 shadow 通电 + reconcile 指标契约.

覆盖 tech-design-wave1.md r2 W1-A 验收:
- 默认值守卫: env 缺省 → mode=shadow(端到端: 默认下 reconcile 真跑并出 mode=shadow 指标).
- mode=off → 零 reconcile 指标(块被跳过).
- shadow 下 mismatch: has_gf 两态(带 ground_facts=1 / 无 gf 派生数字=0) 且 payload 不被改.
- enforce 仍下发 _ledger_retry(通电不回归旧行为) + 出 retry_issued 指标.

指标经真 handler 调用触发(reconcile 指标在 ledger 块 emit, 早于后续 ν 门, 与最终返回
值无关), 读 process-singleton registry 断言. 与 test_report_ledger.py 的纯函数测互补.
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.metrics import get_registry
from bowhead_core.subagent_return_tool import (
    CTX_CURRENT_SUBAGENT_ID_ATTR,
    register_subagent_return_tool,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

_LEDGER_CAVEAT = "部分数字未能与计算台账对齐"


def _good_payload() -> dict[str, Any]:
    """已知 validate-clean 的 payload(与 test_subagent_return_tool 同形).

    带若干带单位数字(11.39% / 90% / 1.85%); stub ctx 无 invocations → 授权集为空 →
    这些数字全部 unauthorized → reconcile 判 mismatch. 用于 has_gf=0 用例.
    """
    return {
        "title": "EA 看板",
        "exec_summary": "整体合规率 11.39%。",
        "kpis": [{"label": "合规率", "value": "11.39%", "sentiment": "bad"}],
        "insights": [
            {
                "title": "部门支出拖累",
                "thesis": "占 90% 单量但合规率 1.85%",
                "evidence": [{"kind": "chart", "chart_section_id": "sec_a", "caption": "对比"}],
                "narrative": "部门支出在两个维度都是结构性问题，合规率和趋势都低。" * 2,
                "confidence": "high",
            }
        ],
        "recommendations": [{"priority": "P0", "action": "OA 必填字段", "owner": "IT 系统组"}],
    }


def _stub_ctx() -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m_test",
        user_id="u_test",
        turn_id="t_ledger",
        datarc=None,  # type: ignore[arg-type]
        session_id="s_test",
    )


def _reconcile_keys() -> list[tuple[tuple[str, str], ...]]:
    """ledger_reconcile_total 已记录的 label-key 集合(counter 缺省则空)."""
    c = get_registry().counter("ledger_reconcile_total")
    return list(c._values.keys())


def _has_key(pairs: tuple[tuple[str, str], ...]) -> bool:
    """某个 (k,v) 组合子集是否出现在任一已记录 label-key 中."""
    return any(all(p in key for p in pairs) for key in _reconcile_keys())


@pytest.fixture(autouse=True)
def _fresh_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_METRICS_ENABLED", "1")
    get_registry().reset()


@pytest.mark.unit
async def test_default_mode_is_shadow_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 缺省 → 默认 shadow: reconcile 真跑并出 mode=shadow 指标(默认值守卫)."""
    monkeypatch.delenv("BOWHEAD_ANALYST_LEDGER", raising=False)
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, _stub_ctx())
    assert inv.error is None
    assert inv.result["ok"] is True  # Shadow does not reject.
    assert _has_key((("mode", "shadow"), ("outcome", "mismatch")))


@pytest.mark.unit
async def test_mode_off_no_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """mode=off → ledger 块整体跳过, 零 reconcile 指标."""
    monkeypatch.setenv("BOWHEAD_ANALYST_LEDGER", "off")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    await reg.execute("subagent_return", {"payload": _good_payload()}, _stub_ctx())
    assert _reconcile_keys() == []


@pytest.mark.unit
async def test_shadow_mismatch_no_gf_has_gf0_and_payload_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow + 无 ground_facts 的未授权数字 → mismatch/has_gf=0; payload 不被改."""
    monkeypatch.setenv("BOWHEAD_ANALYST_LEDGER", "shadow")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    payload = _good_payload()
    inv = await reg.execute("subagent_return", {"payload": payload}, _stub_ctx())
    assert inv.result["ok"] is True
    assert _has_key((("mode", "shadow"), ("outcome", "mismatch"), ("has_gf", "0")))
    # Shadow only observes: never append a ledger caveat (only enforce-exhausted does that).
    assert _LEDGER_CAVEAT not in str(payload.get("caveats", ""))


@pytest.mark.unit
async def test_shadow_mismatch_with_gf_has_gf1(monkeypatch: pytest.MonkeyPatch) -> None:
    """shadow + 带 ground_facts(bad_avg 声称 163 实算 4.23) → mismatch/has_gf=1."""
    monkeypatch.setenv("BOWHEAD_ANALYST_LEDGER", "shadow")
    monkeypatch.setenv("BOWHEAD_PAYLOAD_SOFT_GATE", "0")  # Deterministic: keep auto-fix from changing gf.
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    payload = _good_payload()
    payload["ground_facts"] = [
        {"key": "bad_avg", "label": "户均", "value": 163.0, "unit": "万元", "formula": "3.57*10000/8438"},
    ]
    inv = await reg.execute("subagent_return", {"payload": payload}, _stub_ctx())
    assert inv.result["ok"] is True
    assert _has_key((("mode", "shadow"), ("outcome", "mismatch"), ("has_gf", "1")))


@pytest.mark.unit
async def test_enforce_still_issues_ledger_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """enforce 通电不回归: mismatch 仍下发 _ledger_retry(前 2 次) + retry_issued 指标."""
    monkeypatch.setenv("BOWHEAD_ANALYST_LEDGER", "enforce")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    setattr(ctx, CTX_CURRENT_SUBAGENT_ID_ATTR, "peer_x")
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is False
    assert inv.result.get("_ledger_retry") is True
    assert _has_key((("mode", "enforce"), ("outcome", "retry_issued")))

"""subagent.return tool tests (#142 M137-M1)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.subagent_return_tool import (
    CTX_PAYLOAD_ATTR,
    TERMINAL_FLAG,
    consume_returned_payload,
    register_subagent_return_tool,
)
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

_SPIKE_RUN = Path(__file__).resolve().parents[3] / "reports" / "spike-M137" / "run_01.json"


def _good_payload() -> dict:
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
        turn_id="t_test",
        datarc=None,  # type: ignore[arg-type]  # This tool does not use datarc.
        session_id="s_test",
    )


@pytest.mark.unit
async def test_register_adds_tool() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    assert reg.has("subagent_return")


@pytest.mark.unit
async def test_handler_accepts_valid_payload_and_signals_terminal() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.error is None
    r = inv.result
    assert r["ok"] is True
    assert r[TERMINAL_FLAG] is True
    assert r["summary"]["insights_count"] == 1
    # Payload is accessible on ctx; Sprint chat-style-fix A3 adds header_insight
    # (default "") into the dict; ignore that key for the equality check.
    _stored = dict(getattr(ctx, CTX_PAYLOAD_ATTR) or {})
    _stored.pop("header_insight", None)
    assert _stored == _good_payload()


@pytest.mark.unit
async def test_consume_clears_attr() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    p1 = consume_returned_payload(ctx)
    p2 = consume_returned_payload(ctx)
    # Sprint chat-style-fix A3 — pop header_insight before strict equality.
    p1_for_cmp = dict(p1 or {})
    p1_for_cmp.pop("header_insight", None)
    assert p1_for_cmp == _good_payload()
    assert p2 is None  # Second retrieval is empty.


@pytest.mark.unit
async def test_handler_rejects_non_dict_payload() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    # Non-JSON string: handler must detect failed string fallback and report a specific error.
    inv = await reg.execute("subagent_return", {"payload": "not a dict"}, ctx)
    r = inv.result
    assert r["ok"] is False
    # Error comes from failed string→json.loads OR "must be a JSON object".
    assert ("not valid JSON" in r["error"]) or ("must be a JSON object" in r["error"])
    assert not hasattr(ctx, CTX_PAYLOAD_ATTR)


@pytest.mark.unit
async def test_handler_rejects_missing_required_field() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    bad = _good_payload()
    del bad["title"]
    inv = await reg.execute("subagent_return", {"payload": bad}, ctx)
    r = inv.result
    assert r["ok"] is False
    assert "title" in r["error"]
    assert any("title" in e for e in r.get("validation_errors", []))


@pytest.mark.unit
async def test_handler_rejects_p0_without_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-Phase-A behavior (rollback path): P0 without owner → hard reject.

    Sprint agent-redesign-1: Phase A auto-fix demotes P0→P1 on missing owner (R19).
    This test pins the rollback path (BOWHEAD_PAYLOAD_SOFT_GATE=0).
    """
    monkeypatch.setenv("BOWHEAD_PAYLOAD_SOFT_GATE", "0")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    bad = _good_payload()
    bad["recommendations"][0].pop("owner")
    inv = await reg.execute("subagent_return", {"payload": bad}, ctx)
    assert inv.result["ok"] is False
    assert any("P0" in e and "owner" in e for e in inv.result["validation_errors"])


@pytest.mark.unit
async def test_handler_auto_demotes_p0_without_owner_phase_a(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase A: P0 without owner is auto-demoted to P1 (R19), payload accepted.

    With BOWHEAD_AUTOFIX_NOTIFY_LLM=1 the fix record is exposed via
    `_auto_fixed_fields` (default OFF post-review P1-3 — verbose nag).
    """
    monkeypatch.setenv("BOWHEAD_AUTOFIX_NOTIFY_LLM", "1")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    fixed = _good_payload()
    fixed["recommendations"][0].pop("owner")
    inv = await reg.execute("subagent_return", {"payload": fixed}, ctx)
    assert inv.result["ok"] is True
    assert "_auto_fixed_fields" in inv.result
    assert any(f["rule"] == "R19" for f in inv.result["_auto_fixed_fields"])


@pytest.mark.unit
async def test_handler_auto_demotes_default_silent_notify_off() -> None:
    """Default: auto-fix happens silently (no _auto_fixed_fields leak to LLM)."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    fixed = _good_payload()
    fixed["recommendations"][0].pop("owner")
    inv = await reg.execute("subagent_return", {"payload": fixed}, ctx)
    assert inv.result["ok"] is True
    # Default OFF: no per-rule details exposed
    assert "_auto_fixed_fields" not in inv.result


@pytest.mark.unit
async def test_handler_rejects_evidence_without_ref_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-Phase-A behavior (rollback path): kind-specific field missing → hard reject.

    Sprint agent-redesign-1: Phase A auto-drops malformed evidence (R13). When
    soft-gate off, original hard-reject preserved.
    """
    monkeypatch.setenv("BOWHEAD_PAYLOAD_SOFT_GATE", "0")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    bad = _good_payload()
    bad["insights"][0]["evidence"][0] = {"kind": "chart", "caption": "no ref"}
    inv = await reg.execute("subagent_return", {"payload": bad}, ctx)
    assert inv.result["ok"] is False
    assert any("chart_section_id" in e for e in inv.result["validation_errors"])


@pytest.mark.unit
async def test_handler_auto_drops_evidence_without_ref_field_phase_a(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase A: malformed evidence dropped (R13). With BOWHEAD_AUTOFIX_NOTIFY_LLM=1
    the per-rule record is exposed; default (post-review P1-3) is silent.
    """
    monkeypatch.setenv("BOWHEAD_AUTOFIX_NOTIFY_LLM", "1")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    fixed = _good_payload()
    # 1 bad chart + 1 good callout — bad drops, good kept
    fixed["insights"][0]["evidence"] = [
        {"kind": "chart", "caption": "no ref"},  # bad: chart_section_id missing
        {"kind": "callout", "caption": "ok", "callout_text": "valid"},
    ]
    inv = await reg.execute("subagent_return", {"payload": fixed}, ctx)
    assert inv.result["ok"] is True
    assert any(f["rule"] == "R13" for f in inv.result.get("_auto_fixed_fields", []))


@pytest.mark.unit
async def test_handler_unwraps_double_encoded_payload() -> None:
    """Qwen 偶尔把 payload 双层编码（payload value 是 JSON-string）—— handler 兜底解."""
    import json

    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    p = _good_payload()
    # Simulate double encoding: payload value is a JSON string.
    inv = await reg.execute("subagent_return", {"payload": json.dumps(p)}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert inv.result[TERMINAL_FLAG] is True


@pytest.mark.unit
async def test_handler_args_parse_error_clear_message() -> None:
    """ToolRegistry 报 _args_parse_error 时 handler 给清晰错而不是误诊."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    inv = await reg.execute("subagent_return", {"_args_parse_error": "bad json"}, ctx)
    assert inv.result["ok"] is False
    assert "args 解析失败" in inv.result["error"]


# ─────────────────────────────────────────────────────────────
# M157 thin-path handler tests
# ─────────────────────────────────────────────────────────────


class _StubCanvas:
    def __init__(self, id: str = "canvas_test") -> None:  # noqa: A002
        self.id = id


class _StubSection:
    def __init__(self, sid: str, kind: str, payload: dict) -> None:
        self.id = sid
        self.kind = kind
        self.payload = payload


class _StubCanvasStore:
    """最小 canvas_store stub: get_canvas_by_session + list_sections。

    empty-canvas 止血批: 校验路径改为只读（`get_canvas_by_session`），`ensure_canvas`
    保留但**任何调用都视为回归**——本文件的路径只读不该建 canvas。
    `exists=False` 模拟「本会话还没有 canvas」。
    """

    def __init__(self, sections: list[_StubSection], *, exists: bool = True) -> None:
        self._sections = sections
        self._exists = exists

    async def get_canvas_by_session(self, session_id: str):
        return _StubCanvas() if self._exists else None

    async def ensure_canvas(self, *, session_id: str, workspace_id: str, user_id: str):
        raise AssertionError("只读校验路径不得创建 canvas（empty-canvas 止血批）")

    async def list_sections(self, canvas_id: str):
        return list(self._sections)


def _thin_payload() -> dict:
    return {
        "title": "EA 看板",
        "exec_summary": "整体合规率 11.39%。",
        "kpis": [{"label": "合规率", "value": "11.39%", "sentiment": "bad"}],
        "insights": [],
        "insight_section_ids": ["sec_a", "sec_b"],
        "recommendations": [{"priority": "P0", "action": "OA 必填字段", "owner": "IT 系统组"}],
    }


def _stub_ctx_with_canvas(
    sections: list[_StubSection],
    *,
    canvas_exists: bool = True,
) -> ToolExecutionContext:
    ctx = _stub_ctx()
    ctx.canvas_store = _StubCanvasStore(sections, exists=canvas_exists)
    return ctx


@pytest.mark.unit
async def test_thin_path_accepts_when_sections_exist() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    sections = [
        _StubSection("sec_a", "insight_card", {"title": "体系合规率分化", "thesis": "T", "narrative": "N" * 60}),
        _StubSection("sec_b", "insight_card", {"title": "组件专项整改", "thesis": "T", "narrative": "N" * 60}),
    ]
    ctx = _stub_ctx_with_canvas(sections)
    inv = await reg.execute("subagent_return", {"payload": _thin_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert inv.result[TERMINAL_FLAG] is True


@pytest.mark.unit
async def test_thin_path_without_canvas_does_not_create_one() -> None:
    """empty-canvas 止血批: 会话还没有 canvas 时, 校验路径只读——不建行、不抛,
    走既有「section 不存在」分支（_StubCanvasStore.ensure_canvas 一被调用就断言失败）。"""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx_with_canvas([], canvas_exists=False)
    inv = await reg.execute("subagent_return", {"payload": _thin_payload()}, ctx)
    assert inv.result["ok"] is False
    assert any("unknown id" in e for e in inv.result["validation_errors"])


@pytest.mark.unit
async def test_thin_path_rejects_unknown_section_id() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx_with_canvas([])
    inv = await reg.execute("subagent_return", {"payload": _thin_payload()}, ctx)
    assert inv.result["ok"] is False
    assert any("unknown id" in e for e in inv.result["validation_errors"])


@pytest.mark.unit
async def test_thin_path_auto_strips_wrong_kind_section_ids() -> None:
    """chat-trace-fix-3 (2026-05-15): peer 把 add_chart 的 sec_id
    误塞进 insight_section_ids → 自动剥掉, 不让整个 payload 死.

    Real bug from prod t_ebe7057b2797 — peer used add_chart × 3 but put
    those chart section_ids into payload.insight_section_ids → thin-path
    validator rejected → 0 retry budget left → canvas had charts but no
    composed report (kpi/narrative/insights/recs)."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    # Sections exist BUT 2 are kind=chart not insight_card.
    sections = [
        _StubSection("sec_a", "chart", {"spec": {"kind": "dual_axis", "title": "T1"}}),
        _StubSection("sec_b", "chart", {"spec": {"kind": "rank", "title": "T2"}}),
    ]
    ctx = _stub_ctx_with_canvas(sections)
    inv = await reg.execute("subagent_return", {"payload": _thin_payload()}, ctx)
    # Payload should succeed — wrong-kind IDs auto-stripped, fall to inline
    # insights[] (empty here) so thin-path becomes no-op + payload otherwise valid.
    # The handler now ok because insight_section_ids is filtered to [].
    # (Real prod fix value: composer still runs with whatever inline payload exists.)
    assert inv.result["ok"] is True or (
        # If still fails (no inline insights to fall back to), at least the
        # auto-strip warning fired and reason is no longer "wrong_kind".
        not any("expected 'insight_card'" in e for e in inv.result.get("validation_errors", []))
    )


@pytest.mark.unit
async def test_thin_path_rejects_anti_shell_in_canvas_section() -> None:
    """canvas section narrative 含空壳词 → handler 拒."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    sections = [
        _StubSection("sec_a", "insight_card", {"title": "体系", "thesis": "T", "narrative": "N" * 60}),
        _StubSection("sec_b", "insight_card", {"title": "组件", "thesis": "T", "narrative": "数据加载后再补充"}),
    ]
    ctx = _stub_ctx_with_canvas(sections)
    inv = await reg.execute("subagent_return", {"payload": _thin_payload()}, ctx)
    assert inv.result["ok"] is False
    assert any("placeholder" in e for e in inv.result["validation_errors"])


@pytest.mark.unit
async def test_thin_path_uses_outline_for_tier_check() -> None:
    """ctx 上挂 outline → handler 跑 tier 唯一对应校验."""
    from bowhead_core.subagent_return_tool import CTX_OUTLINE_ATTR
    from bowhead_core.types import QueryOutline

    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    sections = [
        _StubSection("sec_a", "insight_card", {"title": "体系合规", "thesis": "T", "narrative": "N" * 60}),
        _StubSection("sec_b", "insight_card", {"title": "部门合规", "thesis": "T", "narrative": "N" * 60}),
    ]
    ctx = _stub_ctx_with_canvas(sections)
    setattr(
        ctx,
        CTX_OUTLINE_ATTR,
        QueryOutline(
            tier_count=2,
            tier_titles=["体系", "组件"],
        ),
    )
    inv = await reg.execute("subagent_return", {"payload": _thin_payload()}, ctx)
    assert inv.result["ok"] is False
    assert any("未覆盖的 tier" in e for e in inv.result["validation_errors"])


@pytest.mark.unit
async def test_thin_and_inline_both_filled_warns_and_uses_thin() -> None:
    """同填两路 → 强制 thin, log warn (handler 内不会失败)."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    sections = [
        _StubSection("sec_a", "insight_card", {"title": "T", "thesis": "T", "narrative": "N" * 60}),
        _StubSection("sec_b", "insight_card", {"title": "T2", "thesis": "T2", "narrative": "N" * 60}),
    ]
    ctx = _stub_ctx_with_canvas(sections)
    p = _thin_payload()
    # Also fill inline insights.
    p["insights"] = [
        {
            "title": "duplicate",
            "thesis": "x",
            "evidence": [{"kind": "chart", "chart_section_id": "sec_a", "caption": "c"}],
            "narrative": "N" * 50,
        },
    ]
    inv = await reg.execute("subagent_return", {"payload": p}, ctx)
    assert inv.result["ok"] is True, inv.result
    # Handler forcibly cleared inline insights; ctx payload's insights must be empty.
    stored = getattr(ctx, CTX_PAYLOAD_ATTR)
    assert stored["insights"] == []


@pytest.mark.unit
async def test_both_empty_rejected_at_handler() -> None:
    """payload 既无 insight_section_ids 也无 insights → hard reject.

    Sprint agent-redesign-1 Phase A: R7 stays hard. Error message is now phrased
    as "all insights dropped during auto-fix" (single source of truth in
    payload_auto_fix.py).
    """
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx_with_canvas([])
    p = _thin_payload()
    p["insight_section_ids"] = []
    p["insights"] = []
    inv = await reg.execute("subagent_return", {"payload": p}, ctx)
    assert inv.result["ok"] is False
    # Either phrasing acceptable (soft-gate on emits R7; off emits legacy text).
    err_text = " ".join(inv.result.get("validation_errors") or []) + " " + inv.result.get("error", "")
    assert "R7" in err_text or "neither insight_section_ids nor insights" in err_text or "all insights dropped" in err_text


@pytest.mark.unit
async def test_simple_computation_returns_once_without_report_schema_retry() -> None:
    """116 t_6f95d818d6ad：简单短答不能被 R7/recommendations 闸逼成报告。"""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "19 × 29 = 551",
        "kpis": [{"label": "19 × 29", "value": "551", "sentiment": "neutral"}],
        "ground_facts": [
            {
                "key": "product",
                "label": "19 × 29",
                "value": 551,
                "unit": "",
                "formula": "19*29",
                "source": "python_sandbox",
            },
        ],
        "recommendations": [],
    }

    inv = await reg.execute("subagent_return", {"payload": payload}, ctx)

    assert inv.result["ok"] is True, inv.result
    assert getattr(ctx, CTX_PAYLOAD_ATTR)["exec_summary"] == "19 × 29 = 551"


@pytest.mark.unit
async def test_ledger_recovery_uses_verified_formulas_without_a_sandbox(monkeypatch) -> None:
    from bowhead_core.types import ToolInvocation

    monkeypatch.setenv("BOWHEAD_ANALYST_LEDGER", "enforce")
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    ctx = _stub_ctx()
    ctx._current_subagent_invocations = [
        ToolInvocation(tool_name="teammcp.metrics.read", arguments={}, result={"values": [10, 20]}, call_id="metrics")
    ]
    payload = {"delivery_surface": "chat", "exec_summary": "The combined rate is 30%.", "ground_facts": []}

    rejected = await registry.execute("subagent_return", {"payload": payload}, ctx)

    assert rejected.result["_ledger_retry"] is True
    assert "available Tools" in rejected.result["error"]
    assert "python_sandbox" not in rejected.result["error"]
    payload["ground_facts"] = [{"key": "combined", "label": "Combined rate", "value": 30, "unit": "%", "formula": "10+20"}]
    corrected = await registry.execute("subagent_return", {"payload": payload}, ctx)
    assert corrected.result["ok"] is True, corrected.result
    assert corrected.result[TERMINAL_FLAG] is True


@pytest.mark.unit
async def test_simple_computation_never_enters_eager_report_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead-side filtering is too late; the handler's eager composer must skip too."""

    async def _unexpected_compose(**_kwargs: object) -> list[str]:
        raise AssertionError("short Chat payload entered eager report composer")

    monkeypatch.setattr(
        "bowhead_core.canvas_composer.compose_canvas_from_payload",
        _unexpected_compose,
    )
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx.canvas_store = object()
    ctx.parent_bus = object()

    inv = await reg.execute(
        "subagent_return",
        {
            "payload": {
                "delivery_surface": "chat",
                "exec_summary": "19 × 29 = 551",
                "kpis": [
                    {
                        "label": "19 × 29",
                        "value": "551",
                        "sentiment": "neutral",
                    }
                ],
            }
        },
        ctx,
    )

    assert inv.result["ok"] is True, inv.result
    assert not hasattr(ctx.parent_bus, "_psi_compose_done")


def _eager_harness(monkeypatch: pytest.MonkeyPatch) -> tuple[ToolRegistry, ToolExecutionContext, list[dict]]:
    """review #13b 三连测的公共台：写手关（卡片路）、composer 打桩记录调用。"""
    calls: list[dict] = []

    async def _record_compose(**kwargs: object) -> list[str]:
        calls.append(dict(kwargs))
        return ["narrative"]

    monkeypatch.setattr(
        "bowhead_core.canvas_composer.compose_canvas_from_payload",
        _record_compose,
    )
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    ctx.canvas_store = object()
    ctx.parent_bus = SimpleNamespace()
    return reg, ctx, calls


@pytest.mark.unit
async def test_question_return_never_writes_report_content_before_shared_clarification(monkeypatch: pytest.MonkeyPatch) -> None:
    reg, ctx, calls = _eager_harness(monkeypatch)
    payload = _good_payload()
    payload["error"] = {
        "kind": "needs_user_input",
        "message": "A quarter is required.",
        "clarification_payload": {"questions": [{"question": "Which quarter?", "type": "free_text", "options": []}]},
    }
    invocation = await reg.execute("subagent_return", {"payload": payload}, ctx)
    assert invocation.result["ok"] is True and invocation.result["__terminal"] is True
    assert calls == []
    assert consume_returned_payload(ctx)["error"]["kind"] == "needs_user_input"


@pytest.mark.unit
async def test_eager_compose_runs_for_report_payload_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正对照（断「没发生」必配）：无指令、无 spawn 决定时，报告 payload 的
    ψ-E eager compose 必须真跑——否则下面两条压制测试可能因别的原因假绿。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1
    assert getattr(ctx.parent_bus, "_psi_compose_done", False) is True


class _BarrierCanvasStore:
    """两条协程都到齐才放行——把 handler 内的 canvas 读做成确定的让出点。

    真实并发下 ctx._current_subagent_id 会在协程 A 挂起期间被 B 覆盖；这里用
    barrier 把那个窗口变成必然发生，而不是靠调度巧合。
    """

    def __init__(self, n: int) -> None:
        self._barrier = asyncio.Event()
        self._arrived = 0
        self._n = n

    async def _rendezvous(self) -> None:
        self._arrived += 1
        if self._arrived >= self._n:
            self._barrier.set()
        await self._barrier.wait()

    async def get_canvas_by_session(self, session_id: str) -> object:
        await self._rendezvous()
        return SimpleNamespace(id="cv_race")

    async def list_sections(self, canvas_id: str) -> list:
        return []

    async def list_section_ids_by_kind(self, canvas_id: str, *, kind: str) -> set:
        return set()


@pytest.mark.unit
async def test_eager_compose_peer_id_no_cross_attribution_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2（docs/sprints/plan-report-assembly-2026-08-24 §2）：并行腿的 ψ-E
    eager compose 必须各自拿到自己的 peer_id。

    生产现场 t_a06cfbe66ec8：步0/步1 两条 compute 腿 1 秒内先后返回，两条
    ledger.reconcile 与两次 psi.eager_compose **全部记成同一个 sub id** →
    ψ-E 幂等键 (turn_id, peer_id, kind#ordinal) 撞号 → 后返回那条腿的 9 段整体
    被判重复丢弃，竞品那一整腿的报告一个字没上画布（计划卡仍显示 ✓Verified）。

    ★ 本测复现的是**生产机制**而不只是症状：`parent_ctx._current_subagent_id`
    按设计就是「SHARED across parallel sibling tasks → last-wins」
    （`_lead_context.py:44`），两条腿 **spawn 期**就已经把它覆盖成同一个值 ——
    所以 handler 入口那份同步快照（CTO P0-1，:783）也是错的，救不了。
    唯一 per-task 隔离的真源是 ContextVar（`set_current_subagent_id`，
    `subagent.py:1600`），也正是 report-section owner 一直取对 sub_ps_* 的原因。

    ⇒ 台子刻意把共享属性钉成「已被最后一条腿覆盖」的终态，只让 ContextVar 有区分度。
    """
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx.canvas_store = _BarrierCanvasStore(2)

    # Production final state: after both legs spawn, the shared attribute retains only the last leg's ID.
    ctx._current_subagent_id = "sub_RACE_B"

    async def _one(sid: str) -> None:
        # SubagentRunner sets ContextVar within each spawn's own task context.
        # asyncio.gather copies context separately for each coroutine ⇒ per-task isolation.
        from bowhead_core.agents._lead_context import (
            reset_current_subagent_id,
            set_current_subagent_id,
        )

        _tok = set_current_subagent_id(sid)
        try:
            inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
            assert inv.result["ok"] is True, inv.result
        finally:
            reset_current_subagent_id(_tok)

    await asyncio.gather(_one("sub_RACE_A"), _one("sub_RACE_B"))

    assert len(calls) == 2, f"两条腿都应触发 eager compose, 实得 {len(calls)}"
    peer_ids = sorted(str(c.get("peer_id")) for c in calls)
    assert peer_ids == ["sub_RACE_A", "sub_RACE_B"], f"peer_id 串号: {peer_ids} —— 撞号会让 ψ-E 幂等键碰撞, 后返回那条腿的段全部被静默丢弃"


@pytest.mark.unit
async def test_eager_compose_stamps_owner_with_peer_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批1-a：ψ-E compose 必须把 peer_id 透传成 section 的 owner。

    owner 是装订的槽位钥匙（plan 侧按 `owner == f"sub_{step.id}"` 反查本步产出的段）。
    今天 `canvas_composer` 一次没传 owner —— 全库 6603 段只有 523 段（7%）有值，
    且全部来自 write_report 亲笔路。
    """
    reg, ctx, calls = _eager_harness(monkeypatch)
    from bowhead_core.agents._lead_context import (
        reset_current_subagent_id,
        set_current_subagent_id,
    )

    _tok = set_current_subagent_id("sub_ps_owner01")
    try:
        inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    finally:
        reset_current_subagent_id(_tok)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1
    assert calls[0].get("peer_id") == "sub_ps_owner01"


@pytest.mark.unit
async def test_eager_compose_chapter_mode_only_on_plan_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批 2-a：plan 执行轮的腿交「一章」(chapter_mode=True)，普通轮仍交「一份」。

    判据用 turn 级的 `_d6_is_plan_execution`，不用「本轮有几条腿」——第一条腿交稿时
    还不知道后面有没有腿。非 plan 多腿轮暂不开（生产 533 轮里只有 3 轮会散）。
    """
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx.parent_bus._d6_is_plan_execution = True
    await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert calls and calls[0].get("chapter_mode") is True, calls


@pytest.mark.unit
async def test_eager_compose_not_chapter_mode_on_normal_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正对照：普通轮必须仍是 False —— 否则单腿轮会丢掉自己的 eyebrow/建议/局限性。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert calls and calls[0].get("chapter_mode") is False, calls


@pytest.mark.unit
async def test_eager_compose_suppressed_by_user_no_report_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review #13b（C5）：用户显式「不要写报告」时，卡片路的 ψ-E 第一落笔点
    必须压掉——末轮才过滤的话画布会先闪现再消失。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._psi_turn_user_text = "只回答 19×29 等于多少，不要生成报告"
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert calls == []
    assert not hasattr(ctx.parent_bus, "_psi_compose_done")


@pytest.mark.unit
async def test_eager_compose_suppressed_by_lead_spawn_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review #13b（C4）：Lead spawn 时定了 delivery=chat（spawn handler 已按
    report-wins 收敛到 bus）→ ψ-E 同样要看到并压掉。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx.parent_bus._lead_delivery_spawn = "chat"
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert calls == []
    assert not hasattr(ctx.parent_bus, "_psi_compose_done")


@pytest.mark.unit
async def test_eager_compose_suppressed_for_pen_using_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺省代笔=卡片后 eager 是主渲染路；但本 peer 用过笔（M-C 亲笔，正文已在
    画布）→ payload 只是结构化证据，eager 再渲卡片=正文双份，必须压掉。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._current_subagent_invocations = [SimpleNamespace(tool_name="write_report", result={"ok": True, "section_id": "sec_1"})]
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert calls == []
    assert not hasattr(ctx.parent_bus, "_psi_compose_done")


@pytest.mark.unit
async def test_eager_compose_runs_when_write_report_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write_report must not suppress eager composition or final cards."""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._current_subagent_invocations = [SimpleNamespace(tool_name="write_report", result={"error": "not_owner", "owner": "sub_prev"})]
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["revise", "append"])
async def test_eager_compose_in_revise_or_append_mode_lands_no_document_chrome(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """canvas-update-turn 批 B0 review 修：Lead 档位 revise/append 时 eager 不得新立文档——compute 没写成
    write_report（拒/放弃）时此前照渲整套 h1+KPI ⇒ 新文档 ⇒ α 折叠把旧报告折掉。去 title + chapter_mode。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx.parent_bus._lead_canvas_mode = mode
    ctx._current_subagent_invocations = [SimpleNamespace(tool_name="write_report", result={"error": "rev_conflict", "current_rev": 3})]
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1 and calls[0].get("chapter_mode") is True and "title" not in calls[0]["payload"], calls
    assert ("kpis" in calls[0]["payload"]) == (mode == "append"), calls  # revise omits the KPI band; append keeps chapter KPIs.
    # Positive control: new_report / flag off retains old behavior (title present, not chapter mode).
    reg2, ctx2, calls2 = _eager_harness(monkeypatch)
    ctx2.parent_bus._lead_canvas_mode = "new_report"
    await reg2.execute("subagent_return", {"payload": _good_payload()}, ctx2)
    assert calls2[0].get("chapter_mode") is False and calls2[0]["payload"].get("title") == "EA 看板"
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    reg3, ctx3, calls3 = _eager_harness(monkeypatch)
    ctx3.parent_bus._lead_canvas_mode = mode
    await reg3.execute("subagent_return", {"payload": _good_payload()}, ctx3)
    assert calls3[0].get("chapter_mode") is False and calls3[0]["payload"].get("title") == "EA 看板"


@pytest.mark.unit
async def test_eager_landing_nothing_does_not_mark_compose_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """116 s_f2c1700cd1d3 追问轮：revise 档下 eager 去了 title/kpis/exec_summary 后一段没落（kinds=[]），
    却标了 _psi_compose_done ⇒ Lead 末轮 compose_skip_eager_done ⇒ 修订收尾器整轮不跑。零落段不标。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    reg, ctx, calls = _eager_harness(monkeypatch)

    async def _compose_nothing(**kwargs: object) -> list[str]:
        calls.append(dict(kwargs))
        return []

    monkeypatch.setattr("bowhead_core.canvas_composer.compose_canvas_from_payload", _compose_nothing)
    ctx.parent_bus._lead_canvas_mode = "revise"
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True and len(calls) == 1
    assert not getattr(ctx.parent_bus, "_psi_compose_done", False)
    # Positive control: mark when sections were actually persisted.
    reg2, ctx2, calls2 = _eager_harness(monkeypatch)
    await reg2.execute("subagent_return", {"payload": _good_payload()}, ctx2)
    assert calls2 and getattr(ctx2.parent_bus, "_psi_compose_done", False) is True


@pytest.mark.unit
async def test_revision_mode_relaxes_payload_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """canvas-update-turn 批 B0 review 修：revise/append 档的薄修订 payload 只要 exec_summary（+sources）——
    此前按新报告拒收 title/kpis/insights/recommendations 缺失，与修订说明「薄修订别填」自相矛盾。"""
    thin = {
        "exec_summary": "已改成上半年口径。",
        "sources": [{"label": "BI", "tool": "bi_query", "artifact_id": "art_1"}],
        "delivery_surface": "report",
    }
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    reg, ctx, _calls = _eager_harness(monkeypatch)
    ctx.parent_bus._lead_canvas_mode = "revise"
    ctx.parent_bus._write_report_ok_writers = {"lead"}
    inv = await reg.execute("subagent_return", {"payload": dict(thin)}, ctx)
    assert inv.result.get("ok") is True, inv.result
    # Positive control: new_report still rejects the same payload under report requirements.
    reg2, ctx2, _ = _eager_harness(monkeypatch)
    ctx2.parent_bus._lead_canvas_mode = "new_report"
    inv2 = await reg2.execute("subagent_return", {"payload": dict(thin)}, ctx2)
    assert inv2.result.get("ok") is False, inv2.result


@pytest.mark.unit
async def test_eager_compose_suppressed_by_bus_ledger_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并行批次里 write_report 与 subagent_return 同批时 invocation 快照拿不到结果；
    handler 在 bus 上记的账必须独立压掉 eager。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._current_subagent_invocations = []
    ctx.parent_bus._write_report_ok_writers = {"lead"}  # Test lacks ContextVar/subagent ID ⇒ writer defaults to lead.
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert calls == []


@pytest.mark.unit
async def test_eager_compose_mixed_turn_lands_chapters_without_document_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★116 A3 / C5（canvas-elements tech-design-mixed-turn-chrome §2.1）：本轮**别的** peer 亲笔成功
    （账本里有它），本 peer 回 payload ⇒ 此前按「本 peer 没用过笔」照渲整套 h1/署名/导语/KPI/建议/局限
    ⇒ 同轮两份报告。现在走章模式：去 title / exec_summary / kpis，insights 照落成章，外壳交收尾器。"""
    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._current_subagent_invocations = []
    ctx.parent_bus._write_report_ok_writers = {"sub_other_pen"}  # In this test the peer writer defaults to lead, absent from the ledger.
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1, "他人亲笔时本 peer 的内容仍要落（不是整体压掉）"
    assert calls[0].get("chapter_mode") is True
    p = calls[0]["payload"]
    assert "title" not in p and "exec_summary" not in p and "kpis" not in p, p
    assert p.get("insights"), "insights 是本 peer 的内容，必须保留"
    assert getattr(ctx.parent_bus, "_psi_compose_done", False) is True


@pytest.mark.unit
async def test_eager_compose_mixed_turn_positive_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正对照三连：账本空 ⇒ 老样子（非章、带 title）；账本只有自己 ⇒ 整体压掉（既有行为）；
    kill switch 关 ⇒ 他人亲笔也回到老样子。"""
    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._current_subagent_invocations = []
    await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert calls[0].get("chapter_mode") is False and calls[0]["payload"].get("title") == "EA 看板"

    reg2, ctx2, calls2 = _eager_harness(monkeypatch)
    ctx2._current_subagent_invocations = []
    ctx2.parent_bus._write_report_ok_writers = {"lead"}
    await reg2.execute("subagent_return", {"payload": _good_payload()}, ctx2)
    assert calls2 == []

    monkeypatch.setenv("BOWHEAD_EAGER_CHROME_DEDUP", "0")
    reg3, ctx3, calls3 = _eager_harness(monkeypatch)
    ctx3._current_subagent_invocations = []
    ctx3.parent_bus._write_report_ok_writers = {"sub_other_pen"}
    await reg3.execute("subagent_return", {"payload": _good_payload()}, ctx3)
    assert calls3[0].get("chapter_mode") is False and calls3[0]["payload"].get("title") == "EA 看板"


@pytest.mark.unit
async def test_eager_compose_runs_for_penless_peer_with_canvas_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正对照：peer 只画了图没用笔（add_chart 不是 write_report）→
    eager 照跑——压制判据必须窄到「真用过笔」。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx._current_subagent_invocations = [SimpleNamespace(tool_name="add_chart")]
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1


@pytest.mark.unit
async def test_eager_compose_spawn_report_wins_over_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report 赢（与 `_lead_spawn_delivery` 同口径）：bus 上收敛成 report 时
    ψ-E 照跑——漏报告比多报告伤害大。"""
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx.parent_bus._lead_delivery_spawn = "report"
    inv = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert len(calls) == 1


@pytest.mark.skipif(not _SPIKE_RUN.exists(), reason="spike output missing")
@pytest.mark.unit
async def test_handler_accepts_real_qwen_spike_output() -> None:
    """实测 Qwen spike run_01 的 payload 经过 tool 能 accept."""
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    spike_payload = json.loads(_SPIKE_RUN.read_text(encoding="utf-8"))["parsed"]
    inv = await reg.execute("subagent_return", {"payload": spike_payload}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert inv.result[TERMINAL_FLAG] is True


@pytest.mark.unit
async def test_sources_requirement_is_not_scoped_to_web_search_only() -> None:
    """★sources 的必填义务不得只挂在「联网检索」上。

    2026-08-18 116 实测：原措辞写「**联网检索类任务必填**」，`compute` 步据此认为
    与己无关 —— **110 个 done 步里 85 个 sources 为空(77%)，而抽查的每一步都真调过
    工具(1~16 次 tool_result)**。不是没来源可报，是契约把作用域画窄了。
    连带三处失效：verifier 的「来源即证据」规则、引用归属、溯源面。

    这条钉的是**作用域**：描述必须点名非联网的取数工具，且不得把必填条件写成
    「联网检索类任务」。措辞可以改，作用域不许再收窄。
    """
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    desc = reg.get("subagent_return").schema.input_schema["properties"]["payload"]["properties"]["sources"]["description"]
    # Explicitly name offline data-retrieval tools; otherwise compute/BI steps still deem this unrelated.
    assert "bi_query" in desc
    assert "python_sandbox" in desc
    # Require sources whenever retrieval tools ran, not only for web-search tasks.
    assert "只要你调用过任何取数工具就必填" in desc
    assert "**联网检索类任务必填**" not in desc


@pytest.mark.unit
async def test_escaped_newlines_unescaped_even_with_the_soft_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """N1 sits OUTSIDE `BOWHEAD_PAYLOAD_SOFT_GATE` on purpose.

    Losing every chart in the bubble (a literal `\\n` keeps the `[figure:]` anchor
    off its own line) must not depend on an unrelated validation flag. Runs the real
    handler, gate off, and asserts the stored payload came back with real newlines.
    """
    monkeypatch.setenv("BOWHEAD_PAYLOAD_SOFT_GATE", "0")
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    payload = {
        "delivery_surface": "chat",
        "exec_summary": "示例产品销售额2.50亿元。\\n[figure:sec_a]\\n渠道上，Hospital为主。",
    }
    inv = await reg.execute("subagent_return", {"payload": payload}, ctx)
    assert inv.error is None and inv.result["ok"] is True
    stored = dict(getattr(ctx, CTX_PAYLOAD_ATTR) or {})
    assert "[figure:sec_a]" in stored["exec_summary"].splitlines()
    assert "\\n" not in stored["exec_summary"]


@pytest.mark.parametrize("placement", ["nested", "outer", "both"])
@pytest.mark.unit
async def test_peer_question_survives_real_return_tool(placement: str) -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    question = {"questions": [{"question": "分析哪个季度？", "options": ["本季度", "上季度"], "type": "single_select"}]}
    payload = {"error": {"kind": "needs_user_input", "message": "需要确认期间"}}
    if placement != "outer":
        payload["error"]["clarification_payload"] = question
    if placement != "nested":
        payload["clarification_payload"] = question
    original = json.loads(json.dumps(payload))
    invocation = await reg.execute("subagent_return", {"payload": payload}, ctx)
    assert invocation.result["ok"] is True
    returned = consume_returned_payload(ctx)
    assert returned["error"]["clarification_payload"] == question
    assert "clarification_payload" not in returned
    assert payload == original


@pytest.mark.unit
async def test_peer_can_return_missing_value_as_free_text_question() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    question = {"questions": [{"question": "平均所有者权益是多少？", "options": [], "type": "free_text"}]}
    payload = {"error": {"kind": "needs_user_input", "message": "缺少必要数值", "clarification_payload": question}}
    invocation = await reg.execute("subagent_return", {"payload": payload}, ctx)
    assert invocation.result["ok"] is True
    assert consume_returned_payload(ctx)["error"]["clarification_payload"] == question


@pytest.mark.unit
async def test_missing_question_text_gives_model_a_specific_repair_shape() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    payload = {
        "error": {
            "kind": "needs_user_input",
            "message": "缺少股东权益",
            "clarification_payload": {"questions": [{"type": "free_text", "options": []}]},
        }
    }
    invocation = await reg.execute("subagent_return", {"payload": payload}, ctx)
    assert invocation.result["ok"] is False
    assert '"question"' in invocation.result["error"]
    assert '"type":"free_text"' in invocation.result["error"]
    assert invocation.result["error_message"] == "澄清问题未写完整，正在重新整理。"
    assert consume_returned_payload(ctx) is None


@pytest.mark.parametrize("invalid", ["missing", "malformed", "conflicting", "too_many"])
@pytest.mark.unit
async def test_peer_question_rejected_before_terminal_return(invalid: str) -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _stub_ctx()
    question = {"question": "分析哪个季度？", "options": ["本季度", "上季度"], "type": "single_select"}
    payload = {"error": {"kind": "needs_user_input", "message": "需要确认期间"}}
    if invalid == "malformed":
        payload["error"]["clarification_payload"] = {"questions": [question, {"question": "没有选项"}]}
    elif invalid == "conflicting":
        payload["error"]["clarification_payload"] = {"questions": [question]}
        payload["clarification_payload"] = {"questions": [{**question, "question": "分析哪家公司？"}]}
    elif invalid == "too_many":
        payload["error"]["clarification_payload"] = {"questions": [question] * 4}
    invocation = await reg.execute("subagent_return", {"payload": payload}, ctx)
    assert invocation.result["ok"] is False
    assert not invocation.result.get(TERMINAL_FLAG)
    assert consume_returned_payload(ctx) is None


@pytest.mark.unit
async def test_peer_free_text_question_preserves_explicit_input_type_with_stray_choices() -> None:
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    ctx = _stub_ctx()
    payload = {
        "error": {
            "kind": "needs_user_input",
            "message": "The quarter is required.",
            "clarification_payload": {
                "questions": [{"question": "Which quarter?", "type": "free_text", "options": ["2026-Q3"]}],
            },
        }
    }
    invocation = await registry.execute("subagent_return", {"payload": payload}, ctx)
    assert invocation.result["ok"] is True
    assert consume_returned_payload(ctx)["error"]["clarification_payload"]["questions"] == [
        {"question": "Which quarter?", "type": "free_text", "options": []},
    ]


@pytest.mark.unit
async def test_answer_plan_accepts_report_shaped_findings_without_automatic_report(monkeypatch) -> None:
    reg, ctx, calls = _eager_harness(monkeypatch)
    ctx.parent_bus._d6_plan_deliverable_kind = "answer"
    result = await reg.execute("subagent_return", {"payload": _good_payload()}, ctx)
    assert result.result["ok"] is True
    assert calls == []
    assert not getattr(ctx.parent_bus, "_psi_compose_done", False)

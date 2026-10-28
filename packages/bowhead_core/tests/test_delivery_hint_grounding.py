"""A report delivery hint requires grounded user intent.

Lead has not yet seen retrieved data at dispatch time and cannot infer the final
answer volume from the question's shape alone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.subagent import _delivery_report_grounding

pytestmark = pytest.mark.unit


def _ctx(user_text: str = "", memory: str = "", prior_report: bool = False):
    async def _get_canvas_by_session(_sid):
        return SimpleNamespace(id="cv1") if prior_report else None

    async def _list_sections(_cid):
        # Previous turn's visible h1 is the canvas_has_prior_report criterion.
        return [SimpleNamespace(id="s1", kind="h1", payload={"text": "2025 复盘"}, turn_id="t_prev", superseded=False, hidden=False)]

    return SimpleNamespace(
        user_text=user_text,
        memory_pack=SimpleNamespace(executor_block=memory),
        canvas_store=SimpleNamespace(get_canvas_by_session=_get_canvas_by_session, list_sections=_list_sections),
        session_id="s1",
        turn_id="t_now",
    )


async def test_quote_from_user_text_grounds_it() -> None:
    ctx = _ctx(user_text="统计2026年上半年各品牌的营业额，写成一份画布报告")
    assert await _delivery_report_grounding("写成一份画布报告", ctx) == "user_quote"
    # ★ Models insert spaces into Chinese (causing this test's first version to fail); normalization must remove all whitespace.
    assert await _delivery_report_grounding(" 写成一份  画布报告 ", ctx) == "user_quote"


async def test_quote_from_memory_block_grounds_it() -> None:
    ctx = _ctx(user_text="各品牌营业额", memory="<remembered_about_user>- 数据类问题都出成报告</remembered_about_user>")
    assert await _delivery_report_grounding("都出成报告", ctx) == "memory_quote"


async def test_implicit_trend_request_is_ungrounded() -> None:
    """A trend request alone supplies no explicit document-delivery intent."""
    ctx = _ctx(user_text="示例产品甲月度销售趋势")
    assert await _delivery_report_grounding(None, ctx) is None
    assert await _delivery_report_grounding("", ctx) is None
    # Fabricated quote absent from the original ⇒ invalid (the value of the deterministic-fact criterion).
    assert await _delivery_report_grounding("用户要一份报告", ctx) is None


async def test_followup_edit_on_an_existing_report_is_grounded() -> None:
    """Editing an existing report need not repeat the word report in the request."""
    ctx = _ctx(user_text="增长率需要换成数量增长率", prior_report=True)
    assert await _delivery_report_grounding(None, ctx) == "prior_report"


async def test_canvas_mode_grounds_it() -> None:
    ctx = _ctx(user_text="第二章措辞收一下")
    assert await _delivery_report_grounding(None, ctx, canvas_mode="revise") == "canvas_mode"
    assert await _delivery_report_grounding(None, ctx, canvas_mode="append") == "canvas_mode"
    assert await _delivery_report_grounding(None, ctx, canvas_mode="new_report") is None


async def test_too_short_quote_is_not_a_licence() -> None:
    """一个字的"报"到处都能撞上；引文至少要有两个字符才算证据。"""
    ctx = _ctx(user_text="报表在哪")
    assert await _delivery_report_grounding("报", ctx) is None


async def test_lookup_failures_never_ground_it() -> None:
    """取数失败一律按"这条不成立"——不因为查不动画布就放行。"""
    broken = SimpleNamespace(user_text="示例产品甲月度销售趋势", memory_pack=None, canvas_store=None, session_id=None, turn_id="t")
    assert await _delivery_report_grounding("示例产品甲", broken) == "user_quote"  # Quote remains verifiable.
    assert await _delivery_report_grounding(None, broken) is None


# ── Wiring: exercise the real handler once for each state ─────────────────────────────
# Pure-function tests cannot show whether it was called. Drive the real `subagent_spawn` handler here
# to check that Lead's delivery actually reaches the runner.


class _CapturingRunner:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def run(self, **kwargs):
        from bowhead_core.subagent import SubagentResult

        self.kwargs = kwargs
        return SubagentResult(profile="compute", subagent_id="sub_test", ok=True, final_answer="42")


class _FakeBus:
    def __init__(self) -> None:
        self.delivery_spawn = None

    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


async def _spawn_delivery(user_text: str, args: dict) -> str | None:
    """返回最终落到 bus 上的交付面决定（None = hint 被丢弃）。"""
    from bowhead_core.subagent import register_subagent_tool
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    registry = build_default_registry()
    register_subagent_tool(registry)
    runner, bus = _CapturingRunner(), _FakeBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_now",
        datarc=None,
        subagent_runner=runner,
        parent_bus=bus,
        user_text=user_text,
    )
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查月度趋势", **args}, ctx)
    assert inv.error is None, inv.error
    return getattr(bus, "_lead_delivery_spawn", None)


async def test_wiring_shadow_mode_keeps_todays_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """flag 关（默认）＝影子期：只记指标，**行为逐字节不变**——无据的 report 照旧生效。"""
    monkeypatch.delenv("FEATURE_DELIVERY_HINT_GROUNDED", raising=False)
    assert await _spawn_delivery("示例产品甲月度销售趋势", {"delivery": "report"}) == "report"


async def test_wiring_enforced_drops_the_ungrounded_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_DELIVERY_HINT_GROUNDED", "1")
    assert await _spawn_delivery("示例产品甲月度销售趋势", {"delivery": "report"}) is None


async def test_wiring_enforced_keeps_a_grounded_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：同样开着 flag，引得出原话就照常生效——否则上一条证明的只是"开关能关掉一切"。"""
    monkeypatch.setenv("FEATURE_DELIVERY_HINT_GROUNDED", "1")
    got = await _spawn_delivery(
        "统计各品牌营业额，写成一份画布报告",
        {"delivery": "report", "delivery_quote": "写成一份画布报告"},
    )
    assert got == "report"


async def test_wiring_chat_hint_needs_no_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_DELIVERY_HINT_GROUNDED", "1")
    assert await _spawn_delivery("示例产品甲月度销售趋势", {"delivery": "chat"}) == "chat"


async def test_both_branches_are_recorded_so_a_rate_is_computable(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """影子期要的是**比率**，所以有据的那一支也得留痕 —— 只记 ungrounded 只有分子。

    ⚠ 断言落在**日志**上而不是计数器上，因为日志才是 prod 上真读得到的那条路：
    `/metrics` 需要 `BOWHEAD_METRICS_ENDPOINT_ENABLED=1`，116/prod 未设 ⇒ 端点 404，
    且 registry 是进程内 dict、容器重启即清零。
    """
    monkeypatch.delenv("FEATURE_DELIVERY_HINT_GROUNDED", raising=False)
    with caplog.at_level("INFO", logger="bowhead_core.subagent"):
        await _spawn_delivery("示例产品甲月度销售趋势", {"delivery": "report"})
        await _spawn_delivery(
            "统计各品牌营业额，写成一份画布报告",
            {"delivery": "report", "delivery_quote": "写成一份画布报告"},
        )
    hits = [r.getMessage() for r in caplog.records if "lead.delivery_report_grounding" in r.getMessage()]
    assert len(hits) == 2, hits
    assert any("grounded_by=none" in h for h in hits), hits  # Numerator
    assert any("grounded_by=user_quote" in h for h in hits), hits  # Other half of the denominator


# ── Step 2: retain quotes after verification ────────────────────────────────────────
# `delivery_quote` previously fed only criteria/logs; the peer received an **unsourced assertion**
# (the user wants a canvas report), absent from replay events too. The false compliance observed on 116
# (Lead copied the entire question as the quote) exposed this: peers could not verify it, nor could I calculate distributions offline.


async def _spawn_capture(user_text: str, args: dict, *, memory: str = ""):
    """跑真 handler，回传 (runner.kwargs, 发到 bus 的 lead.subagent_summary payload)。"""
    from bowhead_core.subagent import register_subagent_tool
    from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

    emitted: list = []

    class _Bus(_FakeBus):
        async def emit(self, ev):
            emitted.append(ev)

    registry = build_default_registry()
    register_subagent_tool(registry)
    runner, bus = _CapturingRunner(), _Bus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="t_now",
        datarc=None,
        subagent_runner=runner,
        parent_bus=bus,
        user_text=user_text,
    )
    if memory:
        ctx.memory_pack = SimpleNamespace(executor_block=memory)  # type: ignore[attr-defined]
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "查月度趋势", **args}, ctx)
    assert inv.error is None, inv.error
    summary = next(
        (getattr(e, "payload", {}) for e in emitted if getattr(e, "event_type", "") == "lead.subagent_summary"),
        {},
    )
    return runner.kwargs, summary


def _peer_sees(kwargs) -> str:
    return "\n".join(str(v) for v in kwargs.values() if isinstance(v, str))


async def test_verified_quote_reaches_the_peer_with_its_provenance() -> None:
    kwargs, _ = await _spawn_capture(
        "统计各品牌营业额，写成一份画布报告",
        {"delivery": "report", "delivery_quote": "写成一份画布报告"},
    )
    seen = _peer_sees(kwargs)
    assert "用户这次的原话" in seen, seen[:400]
    assert "写成一份画布报告" in seen, seen[:400]


async def test_unverified_quote_is_never_laundered_as_the_users_words() -> None:
    """★ 负对照：核不上的引文**不许**挂「用户原话」的名头递给 peer。

    否则等于把 Lead 的转述（乃至编造）洗成用户说过的话——那比不给出处更糟。
    """
    kwargs, _ = await _spawn_capture(
        "2026年各品牌的月度销售趋势是怎样的",
        {"delivery": "report", "delivery_quote": "用户说要一份正式报告"},  # This sentence is absent from the original.
    )
    seen = _peer_sees(kwargs)
    assert "用户说要一份正式报告" not in seen, seen[:400]
    assert "用户这次的原话" not in seen, seen[:400]
    # Positive control: delivery still reaches the peer (shadow phase preserves behavior), so the checks above do not reflect missing injection.
    assert "delivery" in seen and "report" in seen, seen[:400]


async def test_memory_quote_is_labelled_as_memory_not_as_user_speech() -> None:
    kwargs, _ = await _spawn_capture(
        "各品牌营业额",
        {"delivery": "report", "delivery_quote": "以后销售分析都出一份报告"},
        memory="<remembered_about_user>\n- depth: 以后销售分析都出一份报告\n</remembered_about_user>",
    )
    seen = _peer_sees(kwargs)
    assert "长期偏好" in seen, seen[:400]
    assert "用户这次的原话" not in seen, seen[:400]


async def test_replay_event_carries_the_quote_so_it_can_be_measured_offline() -> None:
    """回放里有引文，离线才算得出「引文相不相关」的分布——那是决定要不要降档的依据。"""
    _, summary = await _spawn_capture(
        "统计各品牌营业额，写成一份画布报告",
        {"delivery": "report", "delivery_quote": "写成一份画布报告"},
    )
    assert summary.get("delivery_quote") == "写成一份画布报告", summary
    assert summary.get("delivery_quote_grounded_by") == "user_quote", summary


async def test_replay_event_records_none_when_the_quote_does_not_check_out() -> None:
    _, summary = await _spawn_capture(
        "2026年各品牌的月度销售趋势是怎样的",
        {"delivery": "report", "delivery_quote": "用户说要一份正式报告"},
    )
    assert summary.get("delivery_quote_grounded_by") == "none", summary

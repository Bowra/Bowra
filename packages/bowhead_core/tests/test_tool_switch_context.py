"""批2/c6 — tool_switch_context 受限 switch 原语契约（PRD §3.4）.

安全不变量：LLM 只传名字；id 解析/授权判定全在 server（可及集合内 match）。
精确/前缀唯一=自动切；substring/歧义=clarification（同 turn 幂等）；集合外=拒；
提交经回调（fail-closed，不改 ctx）；bi cache key 带 workspace 维度。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from bowhead_core.agents.clarification_tool import is_clarification_sentinel
from bowhead_core.datarc_selection import DatarcPersistenceReceipt
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    _bi_cache_key,
    build_default_registry,
)

WS3 = (
    {"id": "1", "name": "商超零售场景"},
    {"id": "234", "name": "北极九章DEMO"},
    {"id": "235", "name": "北极九章TEST"},
)


class _Switcher:
    def __init__(self, fail: bool = False, persisted: bool = True):
        self.calls: list[tuple[str, str]] = []
        self.remembers: list[bool] = []  # framework v1: assert remember passthrough.
        self.fail = fail
        self.persisted = persisted  # framework v1: (member_uid, persisted) return contract.

    async def prepare_selection(self, ws_id, ws_name, *, current):
        self.calls.append((ws_id, ws_name))
        if self.fail:
            raise RuntimeError("Controlled member resolution failure")
        return replace(current, datarc_workspace_id=ws_id, name=ws_name, member_uid=f"member_of_{ws_id}")

    async def save_preference(self, selection, *, remember):
        self.remembers.append(remember)
        return DatarcPersistenceReceipt("saved" if self.persisted else "failed", "default" if remember else "session")


def _ctx(accessible=WS3, switcher=None, active="1", member="m_old") -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace_id="ws_x",
        member_uid=member,
        user_id="u",
        turn_id="t",
        datarc=object(),
        active_datarc_project=active,
        accessible_datarc_workspaces=accessible,
        datarc_context_switcher=switcher,
    )


@pytest.fixture(scope="module")
def reg():
    return build_default_registry()


async def test_exact_unique_switches_ctx_immediately(reg):
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    assert inv.error is None and inv.result["ok"] is True
    assert inv.result["switched"] == {"id": "234", "name": "北极九章DEMO"}
    assert sw.calls == [("234", "北极九章DEMO")]
    # Effective immediately in this turn: replace the full ctx triple.
    assert ctx.active_datarc_project == "234"
    assert ctx.active_datarc_workspace_name == "北极九章DEMO"
    assert ctx.member_uid == "member_of_234"


async def test_prefix_unique_switches(reg):
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "商超"}, ctx)
    assert inv.result["ok"] is True and inv.result["switched"]["id"] == "1"


async def test_prefix_ambiguous_yields_clarification_not_autocommit(reg):
    """「北极九章」前缀命中 2 个 → 绝不静默切，弹确认卡."""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章"}, ctx)
    assert is_clarification_sentinel(inv.result)
    clar = inv.result["clarification"]
    assert clar["kind"] == "tool_context_select"
    assert {o["value"] for o in clar["options"]} == {"234", "235"}
    assert sw.calls == []  # Not submitted.
    assert ctx.active_datarc_project == "1"  # ctx unchanged.


@pytest.mark.parametrize("remember", [False, "false"])
async def test_ambiguous_temporary_switch_defaults_picker_to_session(reg, remember):
    inv = await reg.execute("tool_switch_context", {"name": "北极九章", "remember": remember}, _ctx(switcher=_Switcher()))
    clar = inv.result["clarification"]
    assert clar["remember_option"] is True
    assert clar["remember_default"] is False


async def test_ambiguous_switch_without_remember_defaults_to_session(reg):
    inv = await reg.execute("tool_switch_context", {"name": "北极九章"}, _ctx(switcher=_Switcher()))
    assert inv.result["clarification"]["remember_default"] is False


async def test_ambiguous_switch_with_remember_defaults_to_saved_choice(reg):
    inv = await reg.execute("tool_switch_context", {"name": "北极九章", "remember": True}, _ctx(switcher=_Switcher()))
    assert inv.result["clarification"]["remember_default"] is True


async def test_substring_hit_yields_clarification(reg):
    """「零售」substring 唯一命中也走确认（review P2：防「华东」误切「华东大区」）."""
    ctx = _ctx(switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"name": "零售"}, ctx)
    assert is_clarification_sentinel(inv.result)


async def test_clarification_idempotent_within_turn(reg):
    """同 turn 第二次歧义 → 不再产 sentinel（防注入骚扰刷卡）."""
    ctx = _ctx(switcher=_Switcher())
    inv1 = await reg.execute("tool_switch_context", {"name": "北极九章"}, ctx)
    assert is_clarification_sentinel(inv1.result)
    inv2 = await reg.execute("tool_switch_context", {"name": "北极九章"}, ctx)
    assert not is_clarification_sentinel(inv2.result)
    assert inv2.result["error"] == "pending_user_confirmation"


async def test_outside_accessible_set_refused_with_list(reg):
    ctx = _ctx(switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"name": "医药集团"}, ctx)
    assert inv.result["error"] == "not_accessible"
    assert "商超零售场景" in inv.result["accessible"]
    assert ctx.active_datarc_project == "1"


async def test_hard_miss_typo_gives_did_you_mean(reg):
    """feedback-0713 #2：错别字硬 miss（prod:「商朝零售」vs「商超零售场景」仅一字
    之差, 三级字面匹配全 miss）→ did_you_mean 给高置信建议 + error_kind=
    not_accessible（进预期内 outcome 白名单, 不渲染红色失败步骤）；hint 指引
    模型向用户确认而非擅自切换（保守档）."""
    ctx = _ctx(switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"name": "商朝零售"}, ctx)
    assert inv.result["error"] == "not_accessible"
    assert inv.result["error_kind"] == "not_accessible"
    assert "商超零售场景" in inv.result["did_you_mean"]
    assert "确认" in inv.result["hint"]
    assert ctx.active_datarc_project == "1"  # No switch.


async def test_hard_miss_unrelated_name_empty_did_you_mean(reg):
    """完全无关名 → did_you_mean 空（cutoff 挡低置信建议, 不硬凑误导）."""
    ctx = _ctx(switcher=_Switcher())
    inv = await reg.execute("tool_switch_context", {"name": "医药集团"}, ctx)
    assert inv.result["did_you_mean"] == []


async def test_no_switcher_channel_refused(reg):
    """scheduled/IM 等无回调通道 → 结构化拒绝指路工具面板."""
    ctx = _ctx(switcher=None)
    inv = await reg.execute("tool_switch_context", {"name": "商超零售场景"}, ctx)
    assert inv.result["error"] == "channel_not_supported"


async def test_commit_failure_fail_closed_ctx_unchanged(reg):
    """member 解析失败 → 不改 ctx（绝不「确认了但没换」的反向：换了但身份错）."""
    ctx = _ctx(switcher=_Switcher(fail=True), active="1", member="m_old")
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    assert inv.result["error"] == "member_resolve_failed"
    assert ctx.active_datarc_project == "1"
    assert ctx.member_uid == "m_old"


async def test_remember_default_false_passes_session_semantics(reg):
    """framework v1 §5：不传 remember → 闭包收到 False（现状 session 级语义不变）."""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    assert inv.result["ok"] is True
    assert sw.remembers == [False]
    assert inv.result["effective"] == "session"


async def test_remember_true_passes_through_and_note_says_default(reg):
    """framework v1 §5：remember=true → 闭包收到 True（写 explicit 的耐久改选入口）."""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO", "remember": True}, ctx)
    assert inv.result["ok"] is True
    assert sw.remembers == [True]
    assert inv.result["effective"] == "default"
    # Immediate ctx effect this turn is independent of remember.
    assert ctx.active_datarc_project == "234"


async def test_remember_string_false_not_coerced_truthy(reg):
    """review P1-a：qwen 系把 boolean 发成字符串——"false" 必须按 False 处理，
    绝不 bool("false")=True 把「明确不记住」反转成永久默认."""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO", "remember": "false"}, ctx)
    assert inv.result["ok"] is True
    assert sw.remembers == [False]
    assert inv.result["persistence"]["scope"] == "session"


async def test_remember_string_true_accepted(reg):
    """字符串 "true"（大小写不敏感）按 True 处理——qwen 鲁棒层."""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    await reg.execute("tool_switch_context", {"name": "北极九章DEMO", "remember": "True"}, ctx)
    assert sw.remembers == [True]


async def test_remember_persist_failed_note_honest(reg):
    """review P1-c：持久化失败（persisted=False）时不得宣称「已设为默认」，
    note 如实降级并给出重试指引."""
    sw = _Switcher(persisted=False)
    ctx = _ctx(switcher=sw)
    inv = await reg.execute("tool_switch_context", {"name": "北极九章DEMO", "remember": True}, ctx)
    assert inv.result["ok"] is True  # The switch itself succeeded this turn.
    assert ctx.active_datarc_project == "234"
    assert inv.result["effective"] == "run"
    assert inv.result["persistence"]["status"] == "failed"


def test_bi_cache_key_has_workspace_dimension():
    """review P0-3: 切换后同 query 绝不命中旧工作区缓存."""
    k1 = _bi_cache_key("今年毛利", False, None, None, datarc_workspace="1")
    k2 = _bi_cache_key("今年毛利", False, None, None, datarc_workspace="234")
    assert k1 != k2


# ── bi-workspace-tour: sequential-switch circuit breaker (116 t_619ab802b174: 31 blind switches) ──
async def test_switch_streak_pauses_on_third_consecutive(reg):
    """连续第 3 次 switch（中间零其他工具）→ 预期内暂停：不提交切换、不红。"""
    from bowhead_core.subagent import _is_non_failure_result

    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    r1 = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    r2 = await reg.execute("tool_switch_context", {"name": "北极九章TEST"}, ctx)
    assert r1.result["ok"] is True and r2.result["ok"] is True
    r3 = await reg.execute("tool_switch_context", {"name": "商超零售场景"}, ctx)
    assert r3.result["ok"] is False
    assert r3.result["error_kind"] == "switch_streak_paused"
    assert r3.result["switch_streak"] == 3
    # Not submitted: switcher called only twice; ctx stays at the second target.
    assert len(sw.calls) == 2 and ctx.active_datarc_project == "235"
    # Expected outcome: the actual exemption boundary does not count it as failure.
    assert _is_non_failure_result(r3.result) is True
    # Negative structural check: a magic kind string alone (third-party forged failure) must not be exempt.
    assert _is_non_failure_result({"ok": False, "error_kind": "switch_streak_paused"}) is False


async def test_switch_streak_resets_after_other_tool(reg):
    """「切一个读一个」的合法巡检：中间任何其他工具执行都复位计数。"""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    await reg.execute("tool_switch_context", {"name": "北极九章TEST"}, ctx)
    await reg.execute("tool_list_workspaces", {}, ctx)  # Any non-switch tool.
    r = await reg.execute("tool_switch_context", {"name": "商超零售场景"}, ctx)
    assert r.result["ok"] is True and r.result["switched"]["id"] == "1"


async def test_switch_streak_counts_commits_not_attempts(reg):
    """review r3#1：错别字 miss/查无不占巡检名额——两次 miss 后第 3 次尝试
    恰是第一次真正落地的切换，必须放行而不是被熔断抢答。"""
    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    m1 = await reg.execute("tool_switch_context", {"name": "不存在的工作区甲"}, ctx)
    m2 = await reg.execute("tool_switch_context", {"name": "不存在的工作区乙"}, ctx)
    assert m1.result["error"] == "not_accessible" and m2.result["error"] == "not_accessible"
    r = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    assert r.result["ok"] is True and r.result["switched"]["id"] == "234"


async def test_parallel_direct_switches_preserve_the_winning_revision(reg):
    """Concurrent direct writers cannot overwrite the accepted source revision."""
    import asyncio

    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    invs = await asyncio.gather(
        reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx),
        reg.execute("tool_switch_context", {"name": "北极九章TEST"}, ctx),
        reg.execute("tool_switch_context", {"name": "商超零售场景"}, ctx),
        reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx),
    )
    oks = [i for i in invs if i.result.get("ok") is True]
    conflicts = [i for i in invs if i.result.get("error_kind") == "datarc_selection_conflict"]
    assert len(oks) == 1 and len(conflicts) == 3, [i.result for i in invs]
    assert len(sw.calls) == 1


async def test_switch_streak_box_survives_shallow_copy(reg):
    """review r3#5：载体必须是可变对象（BIOutageLatch 同款教训）——peer 路径的
    copy.copy(ctx) 浅拷贝下计数仍别名共享，熔断不静默失效。"""
    import copy as _copy

    sw = _Switcher()
    ctx = _ctx(switcher=sw)
    await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    await reg.execute("tool_switch_context", {"name": "北极九章TEST"}, ctx)
    ctx2 = _copy.copy(ctx)
    r = await reg.execute("tool_switch_context", {"name": "商超零售场景"}, ctx2)
    assert r.result["error_kind"] == "switch_streak_paused"


async def test_switch_streak_rolls_back_on_switcher_failure(reg):
    """落地失败的切换不占名额：switcher 抛错 → 名额回滚，后续切换不被误熔断。"""
    fail_sw = _Switcher(fail=True)
    ctx = _ctx(switcher=fail_sw)
    for _ in range(3):
        r = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
        assert r.result["error"] == "member_resolve_failed"
    ok_sw = _Switcher()
    object.__setattr__(ctx, "datarc_context_switcher", ok_sw)
    r = await reg.execute("tool_switch_context", {"name": "北极九章DEMO"}, ctx)
    assert r.result["ok"] is True

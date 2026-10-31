"""canvas-update-turn 批 B2（tech-design-b2）——续轮修订的边界缺口。

B2-1：修订 / 追加档的 spawn 结果带 `canvas_changes`（服务端按库现算的本轮画布实际改动），Lead 描述画布只能依据它。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.canvas_revise import (
    CANVAS_CHANGES_DIRECTIVE,
    _mark_folded_by_revise,
    build_canvas_changes,
    removable_chart_ids,
    remove_charts,
)
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

pytestmark = pytest.mark.unit


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


@pytest.fixture
async def store(tmp_path):
    s = CanvasStore(Path(tmp_path) / "b2.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


async def _seed_prior_report(store: CanvasStore) -> dict[str, str]:
    """上一轮（t1）的报告：h1 + 两段正文 + 两张图；另有一张更早就被收起的图、一张 chat 面隐藏的图。"""
    cv = await store.get_canvas_by_session("s1")
    ids: dict[str, str] = {"canvas": cv.id}

    async def add(key: str, kind: str, payload: dict, turn: str = "t1") -> None:
        sec = await store.append_section(canvas_id=cv.id, kind=kind, payload=payload, by_turn_id=turn, owner="sub_a")
        ids[key] = sec.id

    await add("h1", "h1", {"text": "2026年各门店营业额分析报告"})
    await add("trend_text", "narrative", {"markdown": "## 按月趋势分析\n\n全年持续下滑。"})
    await add("rank_text", "narrative", {"markdown": "## 门店排名分析\n\n东湖街居首。"})
    await add("trend_chart", "chart", {"spec": {"title": "2026年1-9月各月营业额趋势"}})
    await add("rank_chart", "chart", {"spec": {"title": "2026年各门店全年营业额排名"}})
    await add("old_folded", "chart", {"spec": {"title": "更早一轮修订收起的图"}}, turn="t0")
    await _mark_folded_by_revise(store, _Bus(), canvas_id=cv.id, turn_id="t1", section_ids=[ids["old_folded"]])
    await add("chat_chart", "chart", {"spec": {"title": "聊天面的图"}}, turn="t_chat")
    await store.mark_turn_hidden_for_chat_mode("t_chat", "chat")
    return ids


async def test_ledger_reports_what_the_database_says(store: CanvasStore) -> None:
    ids = await _seed_prior_report(store)
    # Current t2: revise trend body, new January-June trend chart, fold prior trend chart (replaced).
    await store.patch_section(section_id=ids["trend_text"], patch={"markdown": "## 按月趋势分析\n\n上半年持续下滑。"}, by_turn_id="t2", caller=None)
    new = await store.append_section(canvas_id=ids["canvas"], kind="chart", payload={"spec": {"title": "2026年1-6月趋势"}}, by_turn_id="t2")
    await _mark_folded_by_revise(store, _Bus(), canvas_id=ids["canvas"], turn_id="t2", section_ids=[ids["trend_chart"]])
    # New section streamed further in same turn is added, not a prior-turn revision.
    fresh = await store.append_section(canvas_id=ids["canvas"], kind="narrative", payload={"markdown": "## 新增一节"}, by_turn_id="t2")
    await store.patch_section(section_id=fresh.id, patch={"markdown": "## 新增一节\n\n续写"}, by_turn_id="t2", caller=None)

    cc = await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")

    assert cc is not None
    assert cc["patched"] == [{"section_id": ids["trend_text"], "kind": "narrative", "heading": "按月趋势分析"}]
    assert cc["added_charts"] == [{"section_id": new.id, "title": "2026年1-6月趋势"}]
    assert cc["removed_charts"] == [{"section_id": ids["trend_chart"], "title": "2026年1-9月各月营业额趋势", "reason": "replaced"}]
    # Prior ranking chart not folded ⇒ honestly still visible; exclude earlier-folded and chat-hidden charts.
    assert cc["prior_charts_visible"] == [{"section_id": ids["rank_chart"], "title": "2026年各门店全年营业额排名"}]


async def test_ledger_when_nothing_was_removed(store: CanvasStore) -> None:
    """116 A3 的形状：只改了文字、一张图都没收 ⇒ removed 为空，旧图全在 prior_charts_visible。"""
    ids = await _seed_prior_report(store)
    await store.patch_section(section_id=ids["trend_text"], patch={"markdown": "## 按月趋势分析\n\n（趋势图已删）"}, by_turn_id="t2", caller=None)

    cc = await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")

    assert cc is not None and cc["removed_charts"] == [] and cc["added_charts"] == []
    assert {c["section_id"] for c in cc["prior_charts_visible"]} == {ids["trend_chart"], ids["rank_chart"]}


async def test_ledger_reports_prior_chart_edits_without_claiming_replacement(store: CanvasStore) -> None:
    ids = await _seed_prior_report(store)
    await store.patch_section(section_id=ids["trend_chart"], patch={"spec": {"title": "Updated trend"}}, by_turn_id="t2", caller=None)

    cc = await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")

    assert cc is not None
    assert cc["patched"] == [{"section_id": ids["trend_chart"], "kind": "chart", "heading": "Updated trend"}]
    assert cc["added_charts"] == [] and cc["removed_charts"] == []
    assert {c["section_id"] for c in cc["prior_charts_visible"]} == {ids["trend_chart"], ids["rank_chart"]}


async def test_ledger_unavailable_is_none_not_an_empty_ledger(store: CanvasStore) -> None:
    """取不到就不挂字段：空账单会被读成「什么都没改」。"""
    assert await build_canvas_changes(None, canvas_id="cv", turn_id="t2") is None
    assert await build_canvas_changes(store, canvas_id=None, turn_id="t2") is None
    assert await build_canvas_changes(store, canvas_id="cv", turn_id=None) is None


# ─── Spawn-handler wiring ──────────────────────────────────────


class _PatchingRunner:
    """模拟 compute：跑的过程中就地改一段上一轮正文，然后交回一句话。"""

    def __init__(self, store: CanvasStore, section_id: str) -> None:
        self.store = store
        self.section_id = section_id

    async def run(self, **kwargs):
        await self.store.patch_section(section_id=self.section_id, patch={"markdown": "## 按月趋势分析\n\n改了"}, by_turn_id="parent_t", caller=None)
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"), subagent_id="sub_test", ok=True, final_answer="已按上半年口径修订"
        )


async def _spawn(monkeypatch: pytest.MonkeyPatch, store: CanvasStore, section_id: str, canvas_mode: str) -> dict:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=_PatchingRunner(store, section_id),
        parent_bus=_Bus(),
        session_id="s1",
    )
    ctx.canvas_store = store
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "把报告改成上半年口径", "canvas_mode": canvas_mode}, ctx)
    assert inv.error is None, inv.error
    assert isinstance(inv.result, dict)
    return inv.result


async def test_spawn_result_carries_ledger_and_directive_in_revise_mode(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = await _seed_prior_report(store)
    res = await _spawn(monkeypatch, store, ids["trend_text"], "revise")
    cc = res.get("canvas_changes")
    assert cc is not None, res
    assert [p["section_id"] for p in cc["patched"]] == [ids["trend_text"]]
    assert cc["removed_charts"] == []
    assert {c["section_id"] for c in cc["prior_charts_visible"]} == {ids["trend_chart"], ids["rank_chart"]}
    assert CANVAS_CHANGES_DIRECTIVE in str(res.get("directive") or "")


async def test_spawn_reports_confirmed_in_place_chart_edit(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.types import ToolInvocation

    ids = await _seed_prior_report(store)

    class ChartEditingRunner:
        async def run(self, **kwargs):
            updated = await store.patch_section(
                section_id=ids["trend_chart"], patch={"spec": {"title": "Updated trend"}}, by_turn_id="parent_t", caller=None
            )
            return SubagentResult(
                profile="compute",
                subagent_id="peer",
                ok=True,
                final_answer="",
                payload={"exec_summary": "Updated title", "insights": [], "kpis": [], "recommendations": []},
                tool_invocations=[ToolInvocation(tool_name="edit_chart", arguments={}, result={"section_id": updated.id, "rev": updated.rev})],
            )

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=ChartEditingRunner(),
        parent_bus=_Bus(),
        session_id="s1",
    )
    ctx.canvas_store = store
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Edit the title", "canvas_mode": "revise"}, ctx)
    assert inv.error is None
    assert inv.result["delivered"] == "payload"
    assert inv.result["canvas_changes"]["patched"] == [{"section_id": ids["trend_chart"], "kind": "chart", "heading": "Updated trend"}]
    assert not inv.result["canvas_changes"]["added_charts"] and not inv.result["canvas_changes"]["removed_charts"]
    assert "没有产出任何可交付内容" not in inv.result["directive"]


async def test_spawn_result_has_no_ledger_for_new_report(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = await _seed_prior_report(store)
    res = await _spawn(monkeypatch, store, ids["trend_text"], "new_report")
    assert "canvas_changes" not in res
    assert CANVAS_CHANGES_DIRECTIVE not in str(res.get("directive") or "")


# ─── B2-2：remove_chart ─────────────────────────────────────────────────────


async def test_removable_chart_ids_partition(store: CanvasStore) -> None:
    ids = await _seed_prior_report(store)
    fresh = await store.append_section(canvas_id=ids["canvas"], kind="chart", payload={"spec": {"title": "本轮新图"}}, by_turn_id="t2")
    secs = await store.list_sections(ids["canvas"], include_hidden=True)

    ok, rejected = removable_chart_ids(
        secs,
        [ids["trend_chart"], ids["trend_chart"], ids["trend_text"], ids["old_folded"], fresh.id, "sec_nope", ""],
        this_turn_id="t2",
    )

    assert ok == [ids["trend_chart"]]  # Count duplicates once.
    assert {r["section_id"]: r["reason"] for r in rejected} == {
        ids["trend_text"]: "not_a_chart",
        ids["old_folded"]: "already_removed",
        fresh.id: "drawn_this_turn",
        "sec_nope": "not_found",
    }


async def test_remove_charts_folds_softly_with_companion_and_reason(store: CanvasStore) -> None:
    ids = await _seed_prior_report(store)
    cv = ids["canvas"]
    # Prior trend chart companion code: artifact used only by it ⇒ fold together; ranking companion unchanged.
    await store.patch_section(
        section_id=ids["trend_chart"],
        patch={"spec": {"title": "2026年1-9月各月营业额趋势", "data_artifact_id": "art_trend"}},
        by_turn_id="t1",
        caller=None,
    )
    code = await store.append_section(canvas_id=cv, kind="code_block", payload={"code": "x", "output_artifact_ids": ["art_trend"]}, by_turn_id="t1")
    bus = _Bus()

    out = await remove_charts(store, bus, canvas_id=cv, turn_id="t2", section_ids=[ids["trend_chart"], ids["trend_text"]], reason="removed")

    assert out["removed"] == [ids["trend_chart"]] and out["reason"] == "removed"
    assert [r["section_id"] for r in out["rejected"]] == [ids["trend_text"]] and out["rejected"][0]["hint"]
    by_id = {s.id: s for s in await store.list_sections(cv, include_hidden=True)}
    assert by_id[ids["trend_chart"]].superseded and by_id[code.id].superseded  # Soft fold: row retained.
    assert by_id[ids["trend_chart"]].payload["_meta"] == {"folded_by": "revise", "fold_reason": "removed"}
    assert not by_id[ids["rank_chart"]].superseded and not by_id[ids["trend_text"]].superseded
    sup = [e for e in bus.events if e.event_type == "canvas.sections_superseded"]
    assert sup and sup[-1].payload["folded_by"] == "revise"
    # Ledger records removed honestly, never default replaced.
    cc = await build_canvas_changes(store, canvas_id=cv, turn_id="t2")
    assert cc is not None and cc["removed_charts"] == [{"section_id": ids["trend_chart"], "title": "2026年1-9月各月营业额趋势", "reason": "removed"}]
    assert [c["section_id"] for c in cc["prior_charts_visible"]] == [ids["rank_chart"]]


def _registry(monkeypatch: pytest.MonkeyPatch, flag: str | None):
    if flag is None:
        monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    else:
        monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", flag)
    from bowhead_core.canvas_tools import register_canvas_tools

    reg = build_default_registry()
    register_canvas_tools(reg)  # Called by gateway at startup (main.py).
    return reg


def test_remove_chart_registered_only_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "remove_chart" not in _registry(monkeypatch, None).list_names()
    reg = _registry(monkeypatch, "1")
    assert "remove_chart" in reg.list_names()
    assert reg.get("remove_chart").schema.input_schema["properties"]["reason"]["enum"] == ["replaced", "removed"]


async def _call_remove(monkeypatch: pytest.MonkeyPatch, store: CanvasStore, peer: str, args: dict):
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name

    reg = _registry(monkeypatch, "1")
    ctx = ToolExecutionContext(workspace_id="1", member_uid="m", user_id="u", turn_id="t2", datarc=None, parent_bus=_Bus(), session_id="s1")
    ctx.canvas_store = store
    token = set_current_peer_name(peer)
    try:
        return (await reg.execute("remove_chart", args, ctx)).result
    finally:
        reset_current_peer_name(token)


async def test_remove_chart_tool_for_compute(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = await _seed_prior_report(store)
    bad = await _call_remove(monkeypatch, store, "compute", {"section_ids": [ids["trend_chart"]], "reason": "gone"})
    assert "reason" in str(bad.get("error"))

    done = await _call_remove(monkeypatch, store, "compute", {"section_ids": [ids["trend_chart"]], "reason": "replaced"})
    assert done["removed"] == [ids["trend_chart"]]


async def test_add_chart_reminder_only_in_revise_mode(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from bowhead_core.canvas_revise import note_lead_canvas_mode
    from bowhead_core.canvas_tools import _attach_prior_charts_reminder

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_prior_report(store)
    bus = SimpleNamespace()
    ctx = SimpleNamespace(parent_bus=bus, canvas_store=store, turn_id="t2")

    note_lead_canvas_mode(bus, "append")
    appended: dict = {}
    await _attach_prior_charts_reminder(appended, ctx, ids["canvas"])
    assert appended == {}  # Append chapter: retain all old charts; no notice.

    note_lead_canvas_mode(bus, "revise")
    revised: dict = {"note": "图上只画了 10 / 40 项"}
    await _attach_prior_charts_reminder(revised, ctx, ids["canvas"])
    assert [p["section_id"] for p in revised["prior_charts"]] == [ids["trend_chart"], ids["rank_chart"]]
    assert revised["note"].startswith("图上只画了 10 / 40 项") and "remove_chart" in revised["note"] and ids["rank_chart"] in revised["note"]


def test_remove_chart_is_fenced_from_custom_agents_and_granted_to_live_compute() -> None:
    from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.custom_subagent_store import REPORT_WRITE_TOOLS
    from bowhead_core.subagent_authoring_catalog import NATIVE_ALLOWLIST

    assert "remove_chart" in REPORT_WRITE_TOOLS and "remove_chart" not in NATIVE_ALLOWLIST
    reg = AgentRegistry.bootstrap()
    register_all_system_manifests(reg)
    assert "remove_chart" in reg.get("compute").tools.allow


# ─── Independent review fixes (P1-1 / P1-2 / P1-3 and P2) ────────


async def test_remove_refuses_charts_still_anchored_or_hidden(store: CanvasStore) -> None:
    """P1-1：正文还引用着的图收起后，前端仍按 id 把它渲在正文里、同时又进「已收起」——所以拒。
    chat 面隐藏的图用户在画布上从没见过，按不存在处理。"""
    ids = await _seed_prior_report(store)
    await store.patch_section(
        section_id=ids["rank_text"],
        patch={"markdown": f"## 门店排名分析\n\n[figure:{ids['rank_chart']}]\n\n东湖街居首。"},
        by_turn_id="t1",
        caller=None,
    )
    out = await remove_charts(
        store, _Bus(), canvas_id=ids["canvas"], turn_id="t2", section_ids=[ids["rank_chart"], ids["chat_chart"]], reason="removed"
    )
    assert out["removed"] == []
    assert {r["section_id"]: r["reason"] for r in out["rejected"]} == {ids["rank_chart"]: "still_referenced", ids["chat_chart"]: "not_found"}
    assert "write_report" in next(r["hint"] for r in out["rejected"] if r["reason"] == "still_referenced")
    # Permit removal after changing anchor.
    await store.patch_section(section_id=ids["rank_text"], patch={"markdown": "## 门店排名分析\n\n东湖街居首。"}, by_turn_id="t2", caller=None)
    again = await remove_charts(store, _Bus(), canvas_id=ids["canvas"], turn_id="t2", section_ids=[ids["rank_chart"]], reason="removed")
    assert again["removed"] == [ids["rank_chart"]]


async def test_remove_opens_the_turn_before_its_canvas_frames(store: CanvasStore) -> None:
    """P2：只收了图的一轮，画布事件前要先有一帧 turn_streaming（与 write_report 跨轮改段同口径）。"""
    ids = await _seed_prior_report(store)
    bus = _Bus()  # Recording bus never opens turn frame for caller; tests caller actually opens it.
    await remove_charts(store, bus, canvas_id=ids["canvas"], turn_id="t2", section_ids=[ids["trend_chart"]], reason="replaced")
    frames = [e.event_type for e in bus.events]
    assert frames[0] == "canvas.turn_streaming"
    assert "canvas.section_patched" in frames and frames[-1] == "canvas.sections_superseded"


async def test_ledger_counts_what_the_finisher_is_about_to_fold(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-3：锚从旧图换成本轮新图、没调 remove_chart ⇒ 收尾器紧接着按 B1-2 会收旧图。账单此刻就要记成已收，
    不能说「旧图仍在」（几秒后它就不在了）。收图开关关掉时收尾器不收 ⇒ 账单如实说仍在。"""
    ids = await _seed_prior_report(store)
    await store.patch_section(
        section_id=ids["trend_text"],
        patch={"markdown": f"## 按月趋势分析\n\n[figure:{ids['trend_chart']}]\n\n全年持续下滑。"},
        by_turn_id="t1",
        caller=None,
    )
    new = await store.append_section(canvas_id=ids["canvas"], kind="chart", payload={"spec": {"title": "1-6月趋势"}}, by_turn_id="t2")
    await store.patch_section(
        section_id=ids["trend_text"], patch={"markdown": f"## 按月趋势分析\n\n[figure:{new.id}]\n\n上半年持续下滑。"}, by_turn_id="t2", caller=None
    )

    monkeypatch.setenv("BOWHEAD_CANVAS_REVISE_FOLD_FIGURES", "1")
    cc = await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")
    assert cc is not None
    assert cc["removed_charts"] == [{"section_id": ids["trend_chart"], "title": "2026年1-9月各月营业额趋势", "reason": "replaced"}]
    assert ids["trend_chart"] not in {c["section_id"] for c in cc["prior_charts_visible"]}

    monkeypatch.setenv("BOWHEAD_CANVAS_REVISE_FOLD_FIGURES", "0")
    off = await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")
    assert off is not None and off["removed_charts"] == []
    assert ids["trend_chart"] in {c["section_id"] for c in off["prior_charts_visible"]}


def test_remove_chart_counts_as_a_canvas_write() -> None:
    """P1-2：只收了图的修订轮不能被判「没有产出任何可交付内容」。"""
    from types import SimpleNamespace

    from bowhead_core.subagent import _canvas_write_succeeded
    from bowhead_core.tool_category import is_canvas_tool, lookup_kind

    assert is_canvas_tool("remove_chart") and lookup_kind("remove_chart") == "write"
    ok = SimpleNamespace(tool_name="remove_chart", error=None, result={"ok": True, "removed": ["sec_a"], "reason": "removed"})
    refused = SimpleNamespace(tool_name="remove_chart", error=None, result={"ok": False, "removed": [], "rejected": [{"section_id": "x"}]})
    assert _canvas_write_succeeded(ok) and not _canvas_write_succeeded(refused)


class _ChatAnswerRunner:
    """peer 交回一句 chat 短答（「已删除」）——直达路会把它原样交给用户。``patch_id`` 给了就先真改一段上一轮正文。"""

    def __init__(self, store: CanvasStore | None = None, patch_id: str | None = None) -> None:
        self.store = store
        self.patch_id = patch_id

    async def run(self, **kwargs):
        if self.store is not None and self.patch_id:
            await self.store.patch_section(
                section_id=self.patch_id, patch={"markdown": "## 按月趋势分析\n\n（改了）"}, by_turn_id="parent_t", caller=None
            )
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_chat",
            ok=True,
            final_answer="",
            payload={"delivery_surface": "chat", "exec_summary": "已将月度趋势图从画布中删除。"},
        )


async def test_chat_direct_is_skipped_only_when_the_canvas_actually_changed(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """P2：本轮真动了画布（账单非空）就不走 chat 直达——直达 = peer 自述原样到用户，账单没人读。
    画布一点没动的纯问答续轮照旧直达（不多烧一轮 Lead）；new_report 档是正对照。"""
    ids = await _seed_prior_report(store)
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)

    async def spawn(mode: str, runner: _ChatAnswerRunner, turn: str) -> dict:
        ctx = ToolExecutionContext(
            workspace_id="1", member_uid="m", user_id="u", turn_id=turn, datarc=None,
            subagent_runner=runner, parent_bus=_Bus(), session_id="s1",
        )  # fmt: skip
        ctx.canvas_store = store
        inv = await registry.execute(
            "subagent_spawn", {"profile": "compute", "task": "把月度趋势图删掉", "canvas_mode": mode, "delegation_role": "primary"}, ctx
        )
        assert isinstance(inv.result, dict), inv.error
        return inv.result

    untouched = await spawn("revise", _ChatAnswerRunner(), "t_q")
    assert "canvas_changes" in untouched and "_chat_direct_delivery" in untouched
    changed = await spawn("revise", _ChatAnswerRunner(store, ids["trend_text"]), "parent_t")
    assert changed["canvas_changes"]["patched"] and "_chat_direct_delivery" not in changed
    assert "_chat_direct_delivery" in await spawn("new_report", _ChatAnswerRunner(), "t_n")


async def test_ledger_skips_pending_when_the_finisher_would_not_run(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P2：收尾器在 plan 执行轮 / 本轮新开了文档时不收图——账单也不能先记「已收起」。"""
    from types import SimpleNamespace

    monkeypatch.setenv("BOWHEAD_CANVAS_REVISE_FOLD_FIGURES", "1")
    ids = await _seed_prior_report(store)
    await store.patch_section(
        section_id=ids["trend_text"], patch={"markdown": f"## 按月趋势分析\n\n[figure:{ids['trend_chart']}]"}, by_turn_id="t1", caller=None
    )
    new = await store.append_section(canvas_id=ids["canvas"], kind="chart", payload={"spec": {"title": "新图"}}, by_turn_id="t2")
    await store.patch_section(section_id=ids["trend_text"], patch={"markdown": f"## 按月趋势分析\n\n[figure:{new.id}]"}, by_turn_id="t2", caller=None)

    def removed(cc):
        return [c["section_id"] for c in (cc or {}).get("removed_charts", [])]

    assert removed(await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")) == [ids["trend_chart"]]  # Positive control.
    plan_bus = SimpleNamespace(_d6_is_plan_execution=True)
    assert removed(await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2", bus=plan_bus)) == []
    await store.append_section(canvas_id=ids["canvas"], kind="h1", payload={"text": "另一份新报告"}, by_turn_id="t2")
    assert removed(await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")) == []


class _AnchorSwapRunner:
    """compute 画了新图、把正文锚从旧图换成新图，但没调 remove_chart（B1-2 兜底收图的形状）。"""

    def __init__(self, store: CanvasStore, canvas_id: str, text_id: str) -> None:
        self.store, self.canvas_id, self.text_id = store, canvas_id, text_id

    async def run(self, **kwargs):
        new = await self.store.append_section(canvas_id=self.canvas_id, kind="chart", payload={"spec": {"title": "新图"}}, by_turn_id="parent_t")
        await self.store.patch_section(
            section_id=self.text_id, patch={"markdown": f"## 按月趋势分析\n\n[figure:{new.id}]"}, by_turn_id="parent_t", caller=None
        )
        return SubagentResult(profile="compute", subagent_id="sub_swap", ok=True, final_answer="已换图")


async def test_spawn_passes_the_turn_bus_so_plan_turns_do_not_predict_a_fold(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """账单的收尾器闸要读 turn 级 bus（plan 执行轮标记在上面）——handler 必须把 bus 传进来。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    monkeypatch.setenv("BOWHEAD_CANVAS_REVISE_FOLD_FIGURES", "1")
    ids = await _seed_prior_report(store)
    await store.patch_section(
        section_id=ids["trend_text"], patch={"markdown": f"## 按月趋势分析\n\n[figure:{ids['trend_chart']}]"}, by_turn_id="t1", caller=None
    )
    registry = build_default_registry()
    register_subagent_tool(registry)

    async def spawn(plan: bool) -> dict:
        bus = _Bus()
        bus._d6_is_plan_execution = plan  # type: ignore[attr-defined]
        ctx = ToolExecutionContext(
            workspace_id="1", member_uid="m", user_id="u", turn_id="parent_t", datarc=None,
            subagent_runner=_AnchorSwapRunner(store, ids["canvas"], ids["trend_text"]), parent_bus=bus, session_id="s1",
        )  # fmt: skip
        ctx.canvas_store = store
        inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "换图", "canvas_mode": "revise"}, ctx)
        assert isinstance(inv.result, dict), inv.error
        return inv.result["canvas_changes"]

    assert [c["section_id"] for c in (await spawn(plan=True))["removed_charts"]] == []
    assert ids["trend_chart"] in [c["section_id"] for c in (await spawn(plan=False))["removed_charts"]]  # Positive control.


async def test_hidden_narrative_anchor_does_not_block_removal(store: CanvasStore) -> None:
    """review nit：chat 面隐藏的正文不算「还在引用」（与 B1-2 同口径）。"""
    ids = await _seed_prior_report(store)
    await store.append_section(canvas_id=ids["canvas"], kind="narrative", payload={"markdown": f"[figure:{ids['rank_chart']}]"}, by_turn_id="t_chat2")
    await store.mark_turn_hidden_for_chat_mode("t_chat2", "chat")
    out = await remove_charts(store, _Bus(), canvas_id=ids["canvas"], turn_id="t2", section_ids=[ids["rank_chart"]], reason="removed")
    assert out["removed"] == [ids["rank_chart"]]


# ─── B2-3: missing-argument fallback + preserve original report ──


class _OkRunner:
    async def run(self, **kwargs):
        return SubagentResult(profile="compute", subagent_id="sub_ok", ok=True, final_answer="好")


async def _spawn_args(monkeypatch: pytest.MonkeyPatch, store: CanvasStore, bus: _Bus, extra: dict, *, batch: int | None = None):
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        workspace_id="1", member_uid="m", user_id="u", turn_id="parent_t", datarc=None,
        subagent_runner=_OkRunner(), parent_bus=bus, session_id="s1",
    )  # fmt: skip
    ctx.canvas_store = store
    if batch is not None:
        ctx._lead_spawn_batch_seq = batch  # Production: Lead tool-batch settlement numbers it.
    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "第三章的图改成 1-6 月", **extra}, ctx)
    return inv.result


async def test_missing_canvas_mode_rejected_once_then_defaults_to_append(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 真机：同一句请求连拒 3 次后 Lead 编成「系统暂时无法…请稍后重试」。现在：第一次拒（带「不是系统故障」），
    Lead 下一步仍没给就按 append 放行。"""
    from bowhead_core.canvas_revise import lead_canvas_mode

    await _seed_prior_report(store)
    bus = _Bus()
    first = await _spawn_args(monkeypatch, store, bus, {}, batch=1)
    assert first["error_kind"] == "invalid_args" and "不是系统故障" in first["hint"]
    assert lead_canvas_mode(bus) is None
    second = await _spawn_args(monkeypatch, store, bus, {"canvas_mode": "bogus"}, batch=2)
    assert not second.get("error") and second.get("ok") is True, second
    assert lead_canvas_mode(bus) == "append"
    # New turn/bus restarts count; first still rejected.
    again = await _spawn_args(monkeypatch, store, _Bus(), {}, batch=1)
    assert again.get("error_kind") == "invalid_args"


async def test_sibling_spawns_in_the_same_step_are_both_rejected(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P2-1：同一步里并行的两个 spawn 都缺参——Lead 还没看到拒绝提示，第二个不能替它定档位。"""
    from bowhead_core.canvas_revise import lead_canvas_mode

    await _seed_prior_report(store)
    bus = _Bus()
    a = await _spawn_args(monkeypatch, store, bus, {}, batch=3)
    b = await _spawn_args(monkeypatch, store, bus, {}, batch=3)
    assert a.get("error_kind") == b.get("error_kind") == "invalid_args"
    assert lead_canvas_mode(bus) is None
    c = await _spawn_args(monkeypatch, store, bus, {}, batch=4)  # Still omitted next step ⇒ permit.
    assert c.get("ok") is True and lead_canvas_mode(bus) == "append"


async def test_defaulted_mode_does_not_force_a_report(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P2-2：档位是系统兜底定的、不是 Lead 选的 ⇒ Lead 说的 delivery=chat 照旧，不被强转成报告。
    正对照：Lead 自己选了 append 时照旧收敛成 report。"""
    await _seed_prior_report(store)
    bus = _Bus()
    await _spawn_args(monkeypatch, store, bus, {"delivery": "chat"}, batch=1)
    await _spawn_args(monkeypatch, store, bus, {"delivery": "chat"}, batch=2)
    assert bus._lead_delivery_spawn == "chat"
    chosen = _Bus()
    await _spawn_args(monkeypatch, store, chosen, {"delivery": "chat", "canvas_mode": "append"}, batch=1)
    assert chosen._lead_delivery_spawn == "report"


def test_latest_visible_report_is_the_revision_target() -> None:
    """review P2-3：keep_prior_report 让两份报告同时可见——续轮的修订说明与加章落位都要指向最新那份。"""
    from types import SimpleNamespace

    from bowhead_core.canvas_revise import find_prior_visible_h1, latest_document_block

    old = SimpleNamespace(id="h_old", kind="h1", seq=1, turn_id="t1", superseded=False, hidden_for_chat_mode=False, payload={"text": "旧报告"})
    body_old = SimpleNamespace(
        id="n_old", kind="narrative", seq=2, turn_id="t1", superseded=False, hidden_for_chat_mode=False, payload={"markdown": "旧"}
    )
    new = SimpleNamespace(id="h_new", kind="h1", seq=3, turn_id="t2", superseded=False, hidden_for_chat_mode=False, payload={"text": "新报告"})
    body_new = SimpleNamespace(
        id="n_new", kind="narrative", seq=4, turn_id="t2", superseded=False, hidden_for_chat_mode=False, payload={"markdown": "新"}
    )
    secs = [old, body_old, new, body_new]
    assert find_prior_visible_h1(secs, "t3") is new
    assert find_prior_visible_h1(secs, "t2") is old  # Current-turn h1 excluded.
    assert latest_document_block(secs).h1 is new


async def test_keep_prior_report_only_noted_for_new_report(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import keep_prior_report

    await _seed_prior_report(store)
    kept, revise_bus, plain = _Bus(), _Bus(), _Bus()
    await _spawn_args(monkeypatch, store, kept, {"canvas_mode": "new_report", "keep_prior_report": True})
    await _spawn_args(monkeypatch, store, revise_bus, {"canvas_mode": "revise", "keep_prior_report": True})
    await _spawn_args(monkeypatch, store, plain, {"canvas_mode": "new_report", "keep_prior_report": "true"})  # Boolean True only.
    assert keep_prior_report(kept) and not keep_prior_report(revise_bus) and not keep_prior_report(plain)


async def test_keep_prior_report_skips_latest_wins_fold(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents.lead_orchestrator import _alpha_fold_prior_blocks
    from bowhead_core.canvas_revise import note_keep_prior_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    ids = await _seed_prior_report(store)
    await store.append_section(canvas_id=ids["canvas"], kind="h1", payload={"text": "2026年1-6月报告"}, by_turn_id="t2")
    this_turn = await store.list_sections_appended_by_turn("t2")

    kept = _Bus()
    note_keep_prior_report(kept, True)
    assert await _alpha_fold_prior_blocks(store=store, bus=kept, turn_id="t2", this_turn_sections=this_turn) == []
    assert not {s.id: s for s in await store.list_sections(ids["canvas"])}[ids["h1"]].superseded
    # Positive control: no preservation ⇒ fold old report under older reports.
    assert ids["h1"] in await _alpha_fold_prior_blocks(store=store, bus=_Bus(), turn_id="t2", this_turn_sections=this_turn)


# ─── B2-4: withdraw anchor_heading promise + stripped-anchor counts ──


def test_anchor_heading_no_longer_promises_reordering() -> None:
    """重排从未接上（`build_api_chart_refs` 没有调用方）——三个工具的描述都不能再承诺「放到标题下方 / 重排」。"""
    from bowhead_core.canvas_tools import _add_chart_schema, _add_image_schema, _add_insight_schema

    for schema in (_add_chart_schema(), _add_image_schema(), _add_insight_schema()):
        desc = schema.input_schema["properties"]["anchor_heading"]["description"]
        assert "不会" in desc and "重排" not in desc and "下方" not in desc, (schema.name, desc)
    assert "[figure:" in _add_chart_schema().input_schema["properties"]["anchor_heading"]["description"]


async def test_bake_counts_written_and_stripped_figure_anchors(caplog: pytest.LogCaptureFixture) -> None:
    """§3.6 第 1 步：分清「模型没写锚」与「写了被剥」——每次写入都记写了几个、剥了几个。"""
    import logging
    from types import SimpleNamespace

    from bowhead_core.report_writer_loop import bake_report_markdown

    stats: dict = {}
    md = (
        "## 趋势\n\n全年下滑。\n\n[figure:sec_real]\n\n## 排名\n\n东湖街居首。\n\n[figure:sec_fake]\n\n"
        "如图[figure:sec_ghost]所示。\n\n```\n[figure:sec_real]\n```\n"  # mid-line invalid anchors are stripped too; fenced ones are not references
    )
    with caplog.at_level(logging.INFO, logger="bowhead_core.report_writer_loop"):
        out, _ = await bake_report_markdown(md, ctx=SimpleNamespace(parent_bus=None), figure_ids={"sec_real"}, trace="t9", stats_out=stats)
    assert "sec_fake" not in out and "sec_ghost" not in out
    assert "如图所示。" in out  # the prose around a stripped mid-line anchor survives
    assert stats["figure_anchors_written"] == 4 and stats["figure_anchors_stripped"] == 2
    assert stats["figure_anchors_valid"] == 1  # Only unfenced anchor to real chart.
    assert any("write_report.figure_anchors turn=t9 written=4 stripped=2 valid=1 figure_pool=1" in r.getMessage() for r in caplog.records)
    assert any("invalid_ids=['sec_fake', 'sec_ghost']" in r.getMessage() for r in caplog.records)  # Fenced real ID excluded.

    none: dict = {}
    await bake_report_markdown("## 趋势\n\n全年下滑。", ctx=SimpleNamespace(parent_bus=None), figure_ids={"sec_real"}, stats_out=none)
    assert none["figure_anchors_written"] == none["figure_anchors_stripped"] == none["figure_anchors_valid"] == 0


# ─── B2-5: strict chapter scope + scope-matching KPI updates ─────


async def _seed_plan_report(store: CanvasStore) -> dict[str, str]:
    """三章计划报告（t1）+ 文末综合结论（另一个执行体写、不是章）。116 `s_4fcbcb8219d4` 的形状。"""
    cv = await store.get_canvas_by_session("s1")
    ch_meta = {"composed_by": "eager", "chapter": True}
    ids = {"canvas": cv.id}

    async def add(key: str, kind: str, payload: dict, owner: str | None = None) -> None:
        ids[key] = (await store.append_section(canvas_id=cv.id, kind=kind, payload=payload, by_turn_id="t1", owner=owner)).id

    await add("h1", "h1", {"text": "综合结论：三维复盘"})
    await add("kpi", "kpi_row", {"cells": [{"lbl": "累计营业额", "val": "119.94亿元"}]})
    for n, title in ((1, "2026年门店营业额排名"), (2, "2026年各品类大类营业额对比"), (3, "2026年月度营业额趋势")):
        own = f"sub_ps_{n}"
        await add(f"ch{n}_h", "h1", {"text": title, "_meta": ch_meta}, owner=own)
        await add(f"ch{n}_n", "narrative", {"markdown": f"## {title}\n\n全年 119.94亿元。", "_meta": ch_meta}, owner=own)
    await add("summary", "narrative", {"markdown": "## 综合结论\n\n全年累计 119.94亿元，逐月下滑。"}, owner="sub_ps_final")
    return ids


def test_mixed_plan_doc_lists_non_chapter_sections_grouped_by_heading() -> None:
    """116 dogfood（1.3.46，`s_030bdd27d75a`）：只有月度一步落成了带章 h1 的 plan 章，品类 / 结论是「## 标题 + ### 小节」的普通正文。
    原先只列 plan 章 ⇒ 唯一选项是月度章，「改第二章（品类）」被 Lead 照着清单落到了月度章上。"""
    from types import SimpleNamespace as NS

    from bowhead_core.canvas_revise import resolve_revise_scope, revise_scope_units

    meta = {"composed_by": "eager", "chapter": True}

    def sec(i, seq, kind, payload, owner=None):
        return NS(id=i, seq=seq, kind=kind, turn_id="t1", owner=owner, superseded=False, hidden_for_chat_mode=False, payload=payload)

    secs = [
        sec("doc_h1", 1, "h1", {"text": "2026年零售经营分析报告"}),
        sec("m_h", 3, "h1", {"text": "2026 年逐月营业额趋势", "_meta": meta}, "sub_ps_m"),
        sec("m_n", 5, "narrative", {"markdown": "## 单边下行\n\n…", "_meta": meta}, "sub_ps_m"),
        sec("c_1", 10, "narrative", {"markdown": "## 品类结构：女装单极领跑\n\n…"}, "sub_ps_c"),
        sec("c_2", 11, "narrative", {"markdown": "### 头部贡献\n\n…"}, "sub_ps_c"),
        sec("c_3", 12, "narrative", {"markdown": "没有标题的续段"}, "sub_ps_c"),
        sec("z_1", 18, "narrative", {"markdown": "## 整体结论与建议\n\n…"}, "sub_ps_z"),
        sec("z_2", 19, "narrative", {"markdown": "### 结论一\n\n…"}, "sub_ps_z"),
        sec("cav", 20, "narrative", {"markdown": "**数据局限性**", "style": "caveats"}),
        sec("after", 21, "narrative", {"markdown": "尾部之后一段没标题的正文"}),
    ]
    units = revise_scope_units(secs, this_turn_id="t2")
    assert [u.title for u in units] == ["2026 年逐月营业额趋势", "品类结构：女装单极领跑", "整体结论与建议"]
    assert resolve_revise_scope(units, ["品类结构"]) == ({"c_1", "c_2", "c_3"}, [])  # ## with ### and continuation paragraphs.
    # Exclude limitations section; it closes current group too, so untitled paragraphs after tail never rejoin overall conclusion.
    assert resolve_revise_scope(units, ["整体结论"]) == ({"z_1", "z_2"}, [])


def test_chart_revision_scope_requires_a_unique_user_named_visible_chart() -> None:
    from types import SimpleNamespace

    from bowhead_core.canvas_revise import resolve_chart_revise_scope

    def chart(section_id, title, **state):
        return SimpleNamespace(id=section_id, kind="chart", turn_id="t1", payload={"spec": {"title": title}}, **state)

    charts = [chart("sec_a", "Trend"), chart("sec_b", "Other"), chart("sec_hidden", "Hidden", superseded=True)]

    def resolve(items, text):
        return resolve_chart_revise_scope(charts, items, user_text=text, this_turn_id="t2")

    assert resolve(["Trend"], "Edit chart sec_a") == ({"sec_a"}, [])
    assert resolve(["Trend"], "Edit Trend") == ({"sec_a"}, [])
    assert resolve(["Trend"], "Edit Other") == (set(), ["Trend"])
    assert resolve(["Trend"], "Edit sec_a_extra") == (set(), ["Trend"])
    assert resolve(["Hidden"], "Edit Hidden") == (set(), ["Hidden"])
    charts.append(chart("sec_c", "Trend"))
    assert resolve(["Trend"], "Edit Trend") == (set(), ["Trend"])
    assert resolve(["sec_a"], "Edit sec_a") == ({"sec_a"}, [])


def test_plan_chapter_without_an_h1_is_listed_by_its_first_heading() -> None:
    """116 dogfood（1.3.47，`s_6f8297c12cd1`）：月度章四段都带 chapter meta、没有章 h1——原先要求有 h1 才列，
    整章从清单里消失（清单只剩 3 个单元），它的段也不进章外分组，点名月度章永远对不上。"""
    from types import SimpleNamespace as NS

    from bowhead_core.canvas_revise import resolve_revise_scope, revise_scope_units

    meta = {"composed_by": "eager", "chapter": True}

    def sec(i, seq, kind, payload, owner=None):
        return NS(id=i, seq=seq, kind=kind, turn_id="t1", owner=owner, superseded=False, hidden_for_chat_mode=False, payload=payload)

    secs = [
        sec("doc_h1", 1, "h1", {"text": "2026年全国零售经营全景分析"}),
        sec("store", 3, "narrative", {"markdown": "## 门店营业额排名\n\n…"}, "sub_ps_s"),
        sec("cat", 4, "narrative", {"markdown": "## 各品类大类营业额对比\n\n…"}, "sub_ps_c"),
        sec("m_1", 5, "narrative", {"markdown": "**关键发现**\n\n…", "_meta": meta}, "sub_ps_m"),
        sec("m_2", 6, "narrative", {"markdown": "## 营业额逐月下滑，7月为最大单月跌幅\n\n…", "_meta": meta}, "sub_ps_m"),
        sec("m_3", 7, "narrative", {"markdown": "## 客流量收缩是营业额下滑的主要驱动\n\n…", "_meta": meta}, "sub_ps_m"),
        sec("z_1", 9, "narrative", {"markdown": "## 综合结论与经营建议\n\n…"}, "sub_ps_z"),
    ]
    units = revise_scope_units(secs, this_turn_id="t2")
    assert [u.title for u in units] == ["门店营业额排名", "各品类大类营业额对比", "营业额逐月下滑，7月为最大单月跌幅", "综合结论与经营建议"]
    assert resolve_revise_scope(units, ["营业额逐月下滑"]) == ({"m_1", "m_2", "m_3"}, [])  # Whole chapter, untitled opening included.
    assert resolve_revise_scope(units, ["各品类大类营业额对比"]) == ({"cat"}, [])


def test_plan_chapter_with_a_meaningless_title_is_named_by_its_first_heading() -> None:
    """116 dogfood（1.3.43，`s_549979486a1b`）：plan 章 h1 是「表+insight」——用正文第一个小标题点名，清单也带上它。"""
    from types import SimpleNamespace as NS

    from bowhead_core.canvas_revise import resolve_revise_scope, revise_scope_units

    meta = {"composed_by": "eager", "chapter": True}

    def sec(i, kind, payload, owner):
        return NS(id=i, kind=kind, turn_id="t1", owner=owner, superseded=False, hidden_for_chat_mode=False, payload={**payload, "_meta": meta})

    secs = [
        sec("h_a", "h1", {"text": "表+insight"}, "sub_ps_a"),
        sec("n_a", "narrative", {"markdown": "## 女装一家独大，前三品类吃掉近六成营业额\n\n…"}, "sub_ps_a"),
        sec("h_b", "h1", {"text": "表+insight"}, "sub_ps_b"),
        sec("n_b", "narrative", {"markdown": "导语没有标题"}, "sub_ps_b"),
        sec("n_b2", "narrative", {"markdown": "## 营业额单边下行：1月→9月\n\n…"}, "sub_ps_b"),
    ]
    units = revise_scope_units(secs, this_turn_id="t2")
    assert [(u.title, u.aliases) for u in units] == [
        ("表+insight", ("女装一家独大，前三品类吃掉近六成营业额",)),
        ("表+insight", ("营业额单边下行：1月→9月",)),
    ]
    assert resolve_revise_scope(units, ["女装一家独大"]) == ({"h_a", "n_a"}, [])
    assert resolve_revise_scope(units, ["表+insight"])[1] == ["表+insight"]  # Same-named chapters ⇒ no guess.


async def test_resolve_revise_scope_by_ordinal_title_and_refuses_to_guess(store: CanvasStore) -> None:
    from bowhead_core.canvas_revise import resolve_revise_scope, revise_scope_units

    ids = await _seed_plan_report(store)
    units = revise_scope_units(await store.list_sections(ids["canvas"]), this_turn_id="t2")
    # Three plan chapters + external "## 综合结论" (summary body by another executor, targetable too).
    assert [u.title for u in units] == ["2026年门店营业额排名", "2026年各品类大类营业额对比", "2026年月度营业额趋势", "综合结论"]
    allowed, bad = resolve_revise_scope(units, ["第三章"])  # No heading numbers ⇒ no position count (a chapter may be missing).
    assert allowed == set() and bad == ["第三章"]
    allowed, bad = resolve_revise_scope(units, ["月度营业额趋势"])
    assert allowed == {ids["ch3_h"], ids["ch3_n"]} and bad == []
    allowed, bad = resolve_revise_scope(units, ["「各品类大类营业额对比」"])  # Title fragment, normalize quotes.
    assert allowed == {ids["ch2_h"], ids["ch2_n"]} and bad == []
    allowed, bad = resolve_revise_scope(units, ["2026年", "第四章", "综合结论"])  # Multiple / unnumbered: no guess; summary targetable.
    assert allowed == {ids["summary"]} and bad == ["2026年", "第四章"]


def test_revise_scope_units_fall_back_to_headed_narratives_for_plain_reports() -> None:
    from types import SimpleNamespace

    from bowhead_core.canvas_revise import resolve_revise_scope, revise_scope_units

    def sec(i, md, turn="t1"):
        return SimpleNamespace(id=i, kind="narrative", turn_id=turn, superseded=False, hidden_for_chat_mode=False, payload={"markdown": md})

    secs = [sec("a", "导语，没有标题"), sec("b", "## 按月趋势分析\n\n…"), sec("c", "## 门店排名分析\n\n…"), sec("d", "## 本轮新写", turn="t2")]
    units = revise_scope_units(secs, this_turn_id="t2")
    assert [u.title for u in units] == ["按月趋势分析", "门店排名分析"]
    assert resolve_revise_scope(units, ["门店排名"])[0] == {"c"}


async def _revise_spawn(
    monkeypatch: pytest.MonkeyPatch,
    store: CanvasStore,
    bus: _Bus,
    scope,
    runner=None,
    batch=None,
    *,
    mode="revise",
    resumed=False,
    lang=None,
    user_text="",
):
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    captured: dict = {}

    class _Cap:
        async def run(self, **kwargs):
            captured.update(kwargs)
            return SubagentResult(profile="compute", subagent_id="sub_s", ok=True, final_answer="好")

    ctx = ToolExecutionContext(
        workspace_id="1", member_uid="m", user_id="u", turn_id="t2", datarc=None,
        subagent_runner=runner or _Cap(), parent_bus=bus, session_id="s1",
    )  # fmt: skip
    ctx.canvas_store = store
    ctx.user_text = user_text
    if batch is not None:
        ctx._lead_spawn_batch_seq = batch
    if resumed:
        ctx._resume_turn_id = "t2"  # 用户刚答过一次澄清后的续跑（同一 turn）
    from bowhead_core.effective_language import pin_turn_output_language

    pin_turn_output_language(bus, lang or "zh-CN")  # 产品默认已是 en-US；这批用例按中文界面断言卡片文字
    args = {"profile": "compute", "task": "只把第三章改成 1-6 月"}
    if mode is not None:
        args["canvas_mode"] = mode
    if scope is not None:
        args["revise_scope"] = scope
    inv = await registry.execute("subagent_spawn", args, ctx)
    return inv.result, captured


async def test_spawn_resolves_scope_and_tells_compute(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import out_of_revise_scope

    ids = await _seed_plan_report(store)
    bus = _Bus()
    res, cap = await _revise_spawn(monkeypatch, store, bus, ["月度营业额趋势"])
    assert res.get("ok") is True
    assert not out_of_revise_scope(bus, ids["ch3_n"]) and out_of_revise_scope(bus, ids["summary"]) and out_of_revise_scope(bus, ids["ch1_n"])
    assert "「2026年月度营业额趋势」" in str(cap.get("extra_instructions") or "")


async def test_spawn_admits_only_the_named_chart_in_revision_scope(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import out_of_revise_scope

    ids = await _seed_prior_report(store)
    bus = _Bus()
    title = "2026年1-9月各月营业额趋势"
    result, captured = await _revise_spawn(monkeypatch, store, bus, [title], user_text=f"Edit chart {ids['trend_chart']}")
    assert result.get("ok") is True
    assert not out_of_revise_scope(bus, ids["trend_chart"])
    assert out_of_revise_scope(bus, ids["rank_chart"])
    assert out_of_revise_scope(bus, ids["trend_text"])
    assert title in str(captured.get("extra_instructions") or "")


async def test_explicit_chart_id_does_not_expand_to_a_matching_chapter(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import out_of_revise_scope

    canvas = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "Report"}, by_turn_id="t1")
    chapter = await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": "## Revenue\n\nText."}, by_turn_id="t1")
    chart = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "Revenue"}}, by_turn_id="t1")
    bus = _Bus()
    result, _ = await _revise_spawn(monkeypatch, store, bus, ["Revenue"], user_text=f"Edit chart {chart.id}")
    assert result.get("ok") is True
    assert not out_of_revise_scope(bus, chart.id)
    assert out_of_revise_scope(bus, chapter.id)


async def test_resolved_scopes_in_later_steps_still_dispatch(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照（review 测试 P1-a）：只有「点名对不上」才让后面的步不再派——前后几步都对得上 / 不限范围，照常派。"""
    await _seed_plan_report(store)
    bus = _Bus()
    for batch, scope in ((1, ["月度营业额趋势"]), (2, ["门店营业额排名"]), (3, None)):
        res, cap = await _revise_spawn(monkeypatch, store, bus, scope, batch=batch)
        assert res.get("ok") is True and cap, (batch, res)


async def test_unresolved_scope_asks_the_user_and_blocks_later_dispatches(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户拍板（2026-10-01）：点名的章对不上 ⇒ 由模型 clarification_ask 问用户。116 1.3.56 实测：回清单让 Lead 重给，
    「第五章」被它硬挑成第四部分改掉（t_8c8b80040f9c）、「客户画像」被它去掉范围重派后新写一章（t_eb052e2762c8）。
    ⇒ 本步拒、给现成问法；更晚的步本轮一律不再派活（挑一章、去掉范围、换档位都不行）；同一步并行的兄弟照常判。"""
    await _seed_plan_report(store)
    bus = _Bus()
    first, cap1 = await _revise_spawn(monkeypatch, store, bus, ["第九章"], batch=1)
    assert first["error_kind"] == "invalid_args" and not cap1
    assert "clarification_ask" in first["hint"] and "｜" not in str(first) and "suggested_question" not in first["hint"]
    q = first["suggested_question"]
    # 「第 N 章」类：章可能存在、只是标题里没写编号 ⇒ 不说「没找到」
    assert q["question"].startswith("没法确定「第九章」指的是报告里的哪一部分") and "你想改哪一部分" in q["question"]
    # 三章 + 综合结论 = 4 部分，卡片放不下（每题最多 4 个选项，还要留「新加一章」）⇒ 章名列在问题里、自由回答
    assert q["type"] == "free_text" and q["options"] == [] and len(q["question"]) <= 200
    assert "2026年门店营业额排名；2026年各品类大类营业额对比；2026年月度营业额趋势；综合结论" in q["question"] and "新加一章" in q["question"]
    # 同一步并行的兄弟：自己的范围对得上就照常派
    sibling, cap_s = await _revise_spawn(monkeypatch, store, bus, ["月度营业额趋势"], batch=1)
    assert sibling.get("ok") is True and cap_s
    # 更晚的步：从清单里挑一章 / 去掉范围 / 换别的档位 / 不带档位，一律不再派
    for scope, mode in ((["月度营业额趋势"], "revise"), (None, "revise"), (None, "append"), (None, "new_report"), (None, None)):
        later, cap_l = await _revise_spawn(monkeypatch, store, bus, scope, batch=2, mode=mode)
        assert later["error_kind"] == "invalid_args" and "不要再调用 subagent_spawn" in later["hint"] and not cap_l, (scope, mode)
    assert bus._lead_revise_scope_must_ask_n == 5


async def test_resumed_turn_replies_instead_of_asking_again(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P1：用户刚答过一次澄清的续跑轮里，clarification_ask 会被 resume_guard 拒——此时仍让 Lead 去问，它两头被拒，
    只剩编「已改好」或空转。续跑轮改成：不给问法，直接回复用户、列出现有各部分、不许说改了。"""
    await _seed_plan_report(store)
    bus = _Bus()
    first, cap1 = await _revise_spawn(monkeypatch, store, bus, ["第五部分"], batch=1, resumed=True)
    assert first["error_kind"] == "invalid_args" and not cap1 and "suggested_question" not in first
    assert "不能再问" in first["hint"] and "2026年月度营业额趋势" in first["hint"] and "不要说已经改了" in first["hint"]
    later, _ = await _revise_spawn(monkeypatch, store, bus, None, batch=2, resumed=True)
    assert "不能再问" in later["hint"] and "clarification_ask" not in later["hint"]


async def test_report_without_nameable_parts_ignores_the_scope(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P1：上一轮正文都没有小标题 ⇒ 没有可点名的部分。此前会出一张只有「新加一章」一个选项的卡；现在不按范围拦，照常派。"""
    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "门店营业额"}, by_turn_id="t1")
    await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "全年营业额 119.94亿元，逐月下滑。"}, by_turn_id="t1")
    bus = _Bus()
    res, cap = await _revise_spawn(monkeypatch, store, bus, ["第二章"], batch=1)
    assert res.get("ok") is True and cap
    later, cap_l = await _revise_spawn(monkeypatch, store, bus, None, batch=2)
    assert later.get("ok") is True and cap_l  # 没记成「对不上」，后面的步也照常


async def test_scope_question_follows_the_output_language(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P1：产品默认语言已是英文——卡片文字跟输出语言走，章名保留报告原文。"""
    await _seed_plan_report(store)
    res, _ = await _revise_spawn(monkeypatch, store, _Bus(), ["客户画像"], batch=1, lang="en-US")
    q = res["suggested_question"]
    assert q["question"].startswith("I couldn't find 「客户画像」 in the report") and "2026年月度营业额趋势" in q["question"]


def test_scope_question_shapes() -> None:
    from bowhead_core.canvas_revise import ScopeUnit, revise_scope_question

    few = [ScopeUnit(title=t, section_ids=frozenset({t})) for t in ("门店营业额排名", "品类大类对比")]
    assert revise_scope_question(few, ["客户画像"]) == {
        "question": "报告里没有找到「客户画像」，你想改哪一部分？",
        "type": "single_select",
        "options": ["门店营业额排名", "品类大类对比", "都不是，新加一章"],
    }
    assert revise_scope_question([], ["第二章"]) is None  # 没有可点名的部分 ⇒ 调用方不拦


def test_scope_question_with_long_titles_fits_and_still_resolves() -> None:
    """review P1：内容型章标题动辄 20-35 字，6 部分时整句超 200 字会被 clarification 截掉后面的章名和问句。
    现在截短章名 / 列不下写「等 N 部分」，整句 ≤200；列出的（截短后的）名字仍能唯一对上那一部分；超 80 字的选项被截后也能对上。"""
    from bowhead_core.canvas_revise import ScopeUnit, resolve_revise_scope, revise_scope_question

    titles = [f"第{i}部分的核心发现：门店客流与客单价在二〇二六年上半年出现明显分化{i}" for i in range(1, 7)]
    units = [ScopeUnit(title=t, section_ids=frozenset({f"s{i}"})) for i, t in enumerate(titles, 1)]
    q = revise_scope_question(units, ["客户画像"])
    assert q["type"] == "free_text" and len(q["question"]) <= 200 and "你想改哪一部分" in q["question"]
    listed = q["question"].split("报告里有：", 1)[1].split("。", 1)[0]
    shown = [n.split(" 等 ")[0].rstrip("…") for n in listed.split("；")]
    # 截短后 6 个全列得下（不截短只放得下 5 个、要写「等」）；截短的名字仍能唯一对上
    assert len(shown) == 6 and " 等 " not in q["question"]
    assert all(resolve_revise_scope(units, [n])[1] == [] for n in shown)
    # 部分再多，截短了也放不下 ⇒ 列前几个 + 「等 N 部分」，整句仍 ≤200
    many = [ScopeUnit(title=f"{i:02d}" + "门店客流与客单价分化" * 3, section_ids=frozenset({f"m{i}"})) for i in range(12)]
    qm = revise_scope_question(many, ["客户画像"])
    assert len(qm["question"]) <= 200 and " 等 12 部分" in qm["question"] and qm["question"].rstrip().endswith("「新加一章」。")
    longs = [ScopeUnit(title="长" * 100 + str(i), section_ids=frozenset({f"l{i}"})) for i in range(2)]
    opts = revise_scope_question(longs, ["客户画像"])["options"][:-1]
    from bowhead_core.agents.clarification_tool import normalize_clarification_questions

    cut = normalize_clarification_questions([{"question": "q", "type": "single_select", "options": opts}])[0]["options"]
    assert all(len(o) <= 80 for o in cut)
    # 前 80 字两个选项一样 ⇒ 截断后这一对本就分不出（不猜），但单个超长标题照样对得上
    single = [ScopeUnit(title="长" * 100, section_ids=frozenset({"x"})), ScopeUnit(title="短标题", section_ids=frozenset({"y"}))]
    assert resolve_revise_scope(single, [("长" * 100)[:80]]) == ({"x"}, [])


def test_duplicate_titles_become_distinct_resolvable_options() -> None:
    """review P1：老 plan 报告两章 h1 都是「表+insight」——只用标题当选项，卡片上两个一模一样，选了下一轮仍对不上。
    现在每部分用「能唯一对上它的名字」（这里是正文首个小标题），选哪个都能对上。"""
    from types import SimpleNamespace as NS

    from bowhead_core.canvas_revise import resolve_revise_scope, revise_scope_question, revise_scope_units

    meta = {"composed_by": "eager", "chapter": True}

    def sec(i, kind, payload, owner):
        return NS(id=i, kind=kind, turn_id="t1", owner=owner, superseded=False, hidden_for_chat_mode=False, payload={**payload, "_meta": meta})

    units = revise_scope_units(
        [
            sec("h_a", "h1", {"text": "表+insight"}, "sub_ps_a"),
            sec("n_a", "narrative", {"markdown": "## 女装一家独大，前三品类吃掉近六成营业额\n\n…"}, "sub_ps_a"),
            sec("h_b", "h1", {"text": "表+insight"}, "sub_ps_b"),
            sec("n_b", "narrative", {"markdown": "## 营业额单边下行：1月→9月\n\n…"}, "sub_ps_b"),
        ],
        this_turn_id="t2",
    )
    opts = revise_scope_question(units, ["第五章"])["options"][:-1]
    assert len(opts) == len(set(opts)) == 2 and "表+insight" not in opts
    assert [resolve_revise_scope(units, [o])[0] for o in opts] == [{"h_a", "n_a"}, {"h_b", "n_b"}]
    # 两部分连小标题都没有、只剩同一个标题 ⇒ 卡片上至少不出现两个一模一样的选项
    from bowhead_core.canvas_revise import ScopeUnit

    bare = [ScopeUnit(title="表+insight", section_ids=frozenset({"a"})), ScopeUnit(title="表+insight", section_ids=frozenset({"b"}))]
    assert revise_scope_question(bare, ["第五章"])["options"] == ["表+insight", "都不是，新加一章"]


async def test_scope_naming_every_chapter_is_a_whole_report_revision(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 dogfood（1.3.43，`t_49b2fb04ec3d`）：「把这份报告改成 1-6 月」，Lead 拿到章节清单后把全部章节传回来——
    被当成「只改这几章」，封面 KPI / 标题 / 没有标题的导语段都没改。点满全部章节＝整份修订，不设范围。"""
    from bowhead_core.canvas_revise import out_of_revise_scope, revise_scope_bounded

    ids = await _seed_plan_report(store)
    bus = _Bus()
    res, cap = await _revise_spawn(monkeypatch, store, bus, ["门店营业额排名", "品类大类营业额对比", "月度营业额趋势", "综合结论"])
    assert res.get("ok") is True
    assert not revise_scope_bounded(bus) and not out_of_revise_scope(bus, ids["summary"])
    assert "修订范围" not in str(cap.get("extra_instructions") or "")


async def test_unscoped_sibling_revise_spawn_lifts_the_scope(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import out_of_revise_scope, revise_scope_bounded

    ids = await _seed_plan_report(store)
    bus = _Bus()
    await _revise_spawn(monkeypatch, store, bus, ["月度营业额趋势"])
    assert revise_scope_bounded(bus)  # 前像：限了范围
    await _revise_spawn(monkeypatch, store, bus, None)  # Another same-turn revision peer unbounded ⇒ whole turn unbounded, avoid blocking it.
    assert not out_of_revise_scope(bus, ids["summary"]) and not revise_scope_bounded(bus)


async def test_write_and_read_report_enforce_the_scope(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 真机：「只改第三章」那轮改了文末综合结论。现在范围外的上一轮段 write_report 拒、read_report 标 editable=false。"""
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _read_report, _write_report
    from bowhead_core.canvas_revise import note_revise_scope

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_report(store)
    bus = _Bus()
    note_revise_scope(bus, {ids["ch3_h"], ids["ch3_n"]})
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=bus)
    by_id = {s.id: s for s in await store.list_sections(ids["canvas"])}
    tok = set_current_peer_name("compute")
    try:
        denied = await _write_report(
            {
                "kind": "narrative",
                "content": "## 综合结论\n\n上半年 91.17亿元",
                "section_id": ids["summary"],
                "expected_rev": by_id[ids["summary"]].rev,
            },
            ctx,
        )
        assert denied.get("error") == "out_of_revise_scope", denied
        ok = await _write_report(
            {
                "kind": "narrative",
                "content": "## 2026年月度营业额趋势\n\n上半年 91.17亿元。",
                "section_id": ids["ch3_n"],
                "expected_rev": by_id[ids["ch3_n"]].rev,
            },
            ctx,
        )
        assert ok.get("ok") is True, ok
        outline = {o["section_id"]: o for o in (await _read_report({}, ctx))["outline"]}
    finally:
        reset_current_peer_name(tok)
    assert outline[ids["summary"]]["editable"] is False and outline[ids["ch1_n"]]["editable"] is False
    assert outline[ids["ch3_n"]]["editable"] is True
    assert "上半年" not in {s.id: s for s in await store.list_sections(ids["canvas"])}[ids["summary"]].payload["markdown"]


async def _whole_report_revised_without_kpis(store: CanvasStore) -> dict[str, str]:
    """116 R8：整份改成 1-6 月，正文改了、KPI 带还是全年。"""
    ids = await _seed_plan_report(store)
    for key in ("ch1_n", "ch2_n", "ch3_n", "summary"):
        await store.patch_section(section_id=ids[key], patch={"markdown": "## 改\n\n上半年 91.17亿元。"}, by_turn_id="t2", caller=None)
    return ids


async def test_subagent_return_asks_once_for_new_kpis_when_the_band_is_stale(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import note_lead_canvas_mode

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_registry import ToolRegistry

    await _whole_report_revised_without_kpis(store)
    reg = ToolRegistry()
    register_subagent_return_tool(reg)

    def ctx_for(mode: str, *, revised_prior: bool = True) -> ToolExecutionContext:
        bus = _Bus()
        note_lead_canvas_mode(bus, mode)
        c = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t2", datarc=None, session_id="s1", parent_bus=bus)  # type: ignore[arg-type]
        c.canvas_store = store
        # This peer edited prior-turn body in place this turn (successful write_report section_id); sibling lacks it.
        c._current_subagent_invocations = (
            [SimpleNamespace(tool_name="write_report", arguments={"section_id": "sec_x", "kind": "narrative"}, result={"ok": True})]
            if revised_prior
            else [SimpleNamespace(tool_name="write_report", arguments={"kind": "narrative"}, result={"ok": True})]
        )
        return c

    thin = {"exec_summary": "已改成 1-6 月口径", "delivery_surface": "report"}
    ctx = ctx_for("revise")
    first = (await reg.execute("subagent_return", {"payload": dict(thin)}, ctx)).result
    assert first.get("ok") is False and first.get("_schema_retry") is True and "119.94亿元" in first.get("stale_kpi_values", []), first
    again = (await reg.execute("subagent_return", {"payload": dict(thin)}, ctx)).result
    assert again["ok"] is True  # Reject once per peer.
    with_kpis = (
        await reg.execute("subagent_return", {"payload": {**thin, "kpis": [{"label": "累计", "value": "91.17亿元"}]}}, ctx_for("revise"))
    ).result
    assert with_kpis["ok"] is True
    appended = (await reg.execute("subagent_return", {"payload": dict(thin)}, ctx_for("append"))).result
    assert appended["ok"] is True  # Append leaves KPIs; no request.
    sibling = (await reg.execute("subagent_return", {"payload": dict(thin)}, ctx_for("revise", revised_prior=False))).result
    assert sibling["ok"] is True  # Review P2: sibling without prior-body edit needs no KPI.


async def test_kpi_gate_skips_when_the_finisher_would_not_write_the_band(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P1-1 / P2：点名范围却认不出章（非 plan 文档）或改了多章时，收尾器这一轮不写 KPI 带——要来了也用不上，
    还会诱导 peer 按局部口径重算整条封面带。"""
    from bowhead_core.canvas_revise import kpi_band_will_be_rebuilt, note_revise_scope, stale_kpi_after_revision

    assert kpi_band_will_be_rebuilt([], bounded=False) and kpi_band_will_be_rebuilt([("a", "t1")], bounded=True)
    assert not kpi_band_will_be_rebuilt([], bounded=True) and not kpi_band_will_be_rebuilt([("a", "t1"), ("b", "t1")], bounded=False)
    await _whole_report_revised_without_kpis(store)
    cv = await store.get_canvas_by_session("s1")
    assert await stale_kpi_after_revision(store, canvas_id=cv.id, turn_id="t2") == ["119.94亿元"]  # Positive control: whole document.
    bounded = _Bus()
    note_revise_scope(bounded, {"sec_whatever"})
    assert await stale_kpi_after_revision(store, canvas_id=cv.id, turn_id="t2", bus=bounded) == []


def test_decimal_led_headings_are_not_chapter_numbers() -> None:
    """review P2-1：正文首个小标题常以数字开头（「1.5%增长的原因」「2.3亿元营收」），那是小数不是编号。"""
    from bowhead_core.canvas_revise import ScopeUnit, _title_ordinal, resolve_revise_scope

    assert _title_ordinal("1.5%增长的原因") is None and _title_ordinal("2.3亿元营收") is None
    assert _title_ordinal("2. 品类结构") == 2 and _title_ordinal("二、品类结构") == 2
    units = [ScopeUnit(title="表+insight", section_ids=frozenset({"a"}), aliases=("2.3亿元营收",))]
    assert resolve_revise_scope(units, ["第二章"]) == (set(), ["第二章"])


async def test_single_unit_report_scope_is_not_whole_report(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P2-2：只有一段带标题的非 plan 报告，「只改月度趋势那节」点满了唯一单元——但其余没标题的段不在里面，不能当整份。"""
    from bowhead_core.canvas_revise import out_of_revise_scope, revise_scope_bounded

    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "报告"}, by_turn_id="t1")
    lead = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "导语，没有标题。"}, by_turn_id="t1")
    trend = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "## 月度趋势\n\n全年下滑。"}, by_turn_id="t1")
    bus = _Bus()
    res, _ = await _revise_spawn(monkeypatch, store, bus, ["月度趋势"])
    assert res.get("ok") is True and revise_scope_bounded(bus)
    assert not out_of_revise_scope(bus, trend.id) and out_of_revise_scope(bus, lead.id)


def test_ordinal_prefers_explicit_numbering_in_titles() -> None:
    """review P1-2：非 plan 文档前面有「执行摘要」这类带标题的段时，「第三章」按标题里的「三、」认，不按位置数。"""
    from bowhead_core.canvas_revise import ScopeUnit, resolve_revise_scope

    units = [
        ScopeUnit(title="执行摘要", section_ids=frozenset({"s0"})),
        ScopeUnit(title="一、门店格局", section_ids=frozenset({"s1"})),
        ScopeUnit(title="二、品类结构", section_ids=frozenset({"s2"})),
        ScopeUnit(title="三、月度趋势", section_ids=frozenset({"s3"})),
    ]
    assert resolve_revise_scope(units, ["第三章"]) == ({"s3"}, [])
    assert resolve_revise_scope(units, ["第四章"]) == (set(), ["第四章"])  # Numbering exists ⇒ no positional fallback (4th is "三、…").
    plain = [ScopeUnit(title="门店", section_ids=frozenset({"a"})), ScopeUnit(title="品类", section_ids=frozenset({"b"}))]
    assert resolve_revise_scope(plain, ["第二节"]) == (set(), ["第二节"])  # No numbers ⇒ no positional count; Lead selects by name.


async def test_bounded_scope_on_a_plain_report_leaves_the_cover_alone(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P1-1：非 plan 文档「只改门店排名那节」——收尾器不能按那一节的口径改封面标题 / KPI 带。"""
    from bowhead_core.canvas_revise import note_lead_canvas_mode, note_revise_scope
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_prior_report(store)
    kpi = await store.append_section(
        canvas_id=ids["canvas"], kind="kpi_row", payload={"cells": [{"lbl": "累计", "val": "119.94亿元"}]}, by_turn_id="t1"
    )
    await store.patch_section(section_id=ids["rank_text"], patch={"markdown": "## 门店排名分析\n\n上半年东湖街居首。"}, by_turn_id="t2", caller=None)
    bus = _Bus()
    note_lead_canvas_mode(bus, "revise")
    note_revise_scope(bus, {ids["rank_text"]})
    await compose_report(
        canvas_store=store,
        request=SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id="t2", text="q", language_context=None),
        bus=bus,
        payload={"exec_summary": "改了门店排名", "title": "上半年门店排名", "kpis": [{"label": "累计", "value": "91.17亿元"}]},
        router=None,
    )
    secs = {s.id: s for s in await store.list_sections(ids["canvas"])}
    assert secs[ids["h1"]].rev == 1 and secs[ids["h1"]].payload["text"] == "2026年各门店营业额分析报告"
    assert secs[kpi.id].rev == 1 and secs[kpi.id].payload["cells"] == [{"lbl": "累计", "val": "119.94亿元"}]


async def test_ledger_reports_stale_kpi_and_out_of_scope_text(store: CanvasStore, tmp_path) -> None:
    ids = await _seed_plan_report(store)
    await store.patch_section(
        section_id=ids["ch3_n"], patch={"markdown": "## 2026年月度营业额趋势\n\n上半年 91.17亿元。"}, by_turn_id="t2", caller=None
    )
    cc = await build_canvas_changes(store, canvas_id=ids["canvas"], turn_id="t2")
    assert cc is not None
    assert [x["section_id"] for x in cc["stale_elsewhere"]] == [ids["summary"]] and cc["stale_elsewhere"][0]["values"] == ["119.94亿元"]
    assert "stale_kpi" not in cc  # One chapter edited ⇒ cover KPIs summarize whole; no flag.

    other = CanvasStore(tmp_path / "whole.db")  # Another canvas: whole-scope revision.
    await other.initialize()
    await other.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    whole = await _whole_report_revised_without_kpis(other)
    cc2 = await build_canvas_changes(other, canvas_id=whole["canvas"], turn_id="t2")
    assert cc2 is not None and cc2["stale_kpi"] == ["119.94亿元"]
    cc3 = await build_canvas_changes(other, canvas_id=whole["canvas"], turn_id="t2", payload={"kpis": [{"label": "累计", "value": "91.17亿元"}]})
    assert cc3 is not None and "stale_kpi" not in cc3  # New KPIs ⇒ finisher rebuilds row, no residue.


# ─── B2-6: resolve ν1.5 integer body anchors to actual IDs before writes ──


async def test_write_report_translates_simple_int_figure_anchors(store: CanvasStore, caplog: pytest.LogCaptureFixture) -> None:
    """116 dogfood（1.3.46）：`written=2 stripped=2 invalid_ids=['15','17']`——写手用本 run 的 ν1.5 编号写图锚，bake 全剥，
    图与正文零挂钩。现在写入前用这个 run 自己那份表翻：图的号 → `sec_*`；数据的号不翻（照剥，不把锚绑到 art_ 上）。
    ctx 上那份是并行 sibling 共享、后写者赢的表——同一个号在那边指向**别的**图，读它就会绑错。"""
    import logging

    from bowhead_core._ref_allowlist import RefAllowlist
    from bowhead_core.agents._lead_context import (
        reset_current_peer_name,
        reset_current_ref_allowlist,
        set_current_peer_name,
        set_current_ref_allowlist,
    )
    from bowhead_core.agents.report_tools import _write_report

    cv = await store.get_canvas_by_session("s1")
    chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"title": "月度趋势"}}, by_turn_id="t1")
    al = RefAllowlist()
    n_data = al.add("data", "art_abc")
    n_chart = al.add("chart", chart.id)
    sibling_chart = await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {"title": "门店排名"}}, by_turn_id="t1")
    sibling = RefAllowlist()
    sibling.add("chart", "sec_unrelated")
    assert sibling.add("chart", sibling_chart.id) == n_chart  # Same number, different chart in sibling table.
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t1", workspace_id="w", user_id="u", parent_bus=_Bus(), _ν15_allowlist=sibling)
    tok = set_current_peer_name("compute")
    al_tok = set_current_ref_allowlist(al)
    try:
        with caplog.at_level(logging.INFO, logger="bowhead_core.report_writer_loop"):
            res = await _write_report(
                {"kind": "narrative", "content": f"## 趋势\n\n全年下滑。\n\n[figure:{n_chart}]\n\n## 数据\n\n见表。\n\n[figure:{n_data}]"}, ctx
            )
    finally:
        reset_current_ref_allowlist(al_tok)
        reset_current_peer_name(tok)
    assert res.get("ok") is True, res
    md = "\n".join(str(s.payload.get("markdown") or "") for s in await store.list_sections(cv.id) if s.kind == "narrative")
    assert (
        f"[figure:{chart.id}]" in md and f"[figure:{sibling_chart.id}]" not in md and "[figure:art_abc]" not in md and f"[figure:{n_data}]" not in md
    )
    assert any("valid=1" in r.getMessage() and f"invalid_ids=['{n_data}']" in r.getMessage() for r in caplog.records)


async def test_parallel_spawns_each_see_their_own_ref_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个 run 并行、共用同一个 parent_ctx：`parent_ctx._ν15_allowlist` 后写者赢，而写入口读到的必须是**各自**那份表，
    run 结束后还原（不漏给 Lead）。钉的是 spawn 处的接线——只测 `_write_report` 的话，spawn 不挂表也全绿。"""
    from typing import Any

    import anyio
    import bowhead_core.subagent as subagent_mod
    from bowhead_core.agents._lead_context import get_current_ref_allowlist
    from bowhead_core.events import TurnEventBus
    from bowhead_core.subagent import PROFILES, SubagentProfile, SubagentRunner

    from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, TokenUsage, ToolCall

    monkeypatch.setenv("BOWHEAD_REF_ALLOWLIST_INJECT", "1")
    monkeypatch.setitem(
        PROFILES, "lead", SubagentProfile(name="lead", llm_agent_type="lead", tool_whitelist=("kb_search",), system_prompt="x", max_iterations=4)
    )

    def _router() -> Router:
        a = MockAdapter(provider_name="p1")
        usage = TokenUsage(input_tokens=1, output_tokens=1, cached_tokens=0)
        a.scripted_responses = [
            LLMResponse(content="", tool_calls=[ToolCall(id="c0", name="kb_search", arguments={"query": "q"})], usage=usage),
            LLMResponse(content="完", usage=usage),
        ]
        reg = ProviderRegistry()
        reg.load([ProviderConfig(name="p1", type="mock")])
        reg._adapters["p1"] = a  # type: ignore[attr-defined]
        r = Router(reg)
        r.bind(AgentBinding(agent_type="lead", primary=("p1", "mock")))
        return r

    parent_ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t1", datarc=None, session_id="s1")  # type: ignore[arg-type]
    seen: dict[str, tuple[Any, Any]] = {}
    both_inside = anyio.Event()
    inside: list[str] = []

    async def _one(name: str) -> None:
        runner = SubagentRunner(router=_router(), tool_registry=build_default_registry(), backend="legacy")

        async def _exec(tc: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG001
            inside.append(name)
            if len(inside) == 2:
                both_inside.set()
            await both_inside.wait()  # Read after both runs attach tables ⇒ shared attr holds only one.
            seen[name] = (get_current_ref_allowlist(), getattr(parent_ctx, "_ν15_allowlist", None))
            return subagent_mod.ToolInvocation(tool_name=tc.name, arguments=tc.arguments, result={"ok": True, "chunks": []}, error=None)

        runner._execute_tool = _exec  # type: ignore[method-assign]
        await runner.run(profile_name="lead", task=name, parent_turn_id="t1", parent_bus=TurnEventBus(), parent_ctx=parent_ctx)

    with anyio.fail_after(20):
        async with anyio.create_task_group() as tg:
            tg.start_soon(_one, "a")
            tg.start_soon(_one, "b")
    (own_a, shared_a), (own_b, shared_b) = seen["a"], seen["b"]
    assert own_a is not None and own_b is not None and own_a is not own_b
    # Positive control: at least one shared-attr reader loses own table (last wins; first finisher clears it, leaving None for other).
    assert not (shared_a is own_a and shared_b is own_b)
    assert get_current_ref_allowlist() is None


# ─── B2-7: in-place prior plan-chapter titles (report title still payload.title) ──


async def test_writer_can_retitle_a_prior_chapter_but_not_the_document_title(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 dogfood（1.3.49，`s_18c8b86f3003`）：整份改成 1-6 月后章标题仍是「2026年1-9月…」——写手对 h1 只拿到 kind_not_editable。
    现在章标题可改：只写 payload.text（h1 渲染读的键），不进正文 bake；文档大标题仍拒（它由收尾器按 payload.title 换）。"""
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _read_report, _write_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_report(store)
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=_Bus())
    by_id = {s.id: s for s in await store.list_sections(ids["canvas"])}
    tok = set_current_peer_name("compute")
    try:
        outline = {o["section_id"]: o for o in (await _read_report({}, ctx))["outline"]}
        ok = await _write_report(
            {"kind": "narrative", "content": "# 2026年1-6月营业额趋势\n", "section_id": ids["ch3_h"], "expected_rev": by_id[ids["ch3_h"]].rev},
            ctx,
        )
        doc = await _write_report(
            {"kind": "narrative", "content": "上半年复盘", "section_id": ids["h1"], "expected_rev": by_id[ids["h1"]].rev},
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert outline[ids["ch3_h"]]["editable"] is True and outline[ids["ch3_h"]]["title"] == "2026年月度营业额趋势"
    assert outline[ids["h1"]]["editable"] is False
    assert ok.get("ok") is True and ok["title"] == "2026年1-6月营业额趋势", ok
    assert doc.get("error") == "kind_not_editable", doc
    after = {s.id: s for s in await store.list_sections(ids["canvas"])}
    assert after[ids["ch3_h"]].payload["text"] == "2026年1-6月营业额趋势" and "markdown" not in after[ids["ch3_h"]].payload
    assert after[ids["ch3_h"]].payload["_meta"]["chapter"] is True  # Shallow merge: retain chapter meta.
    assert after[ids["h1"]].payload["text"] == "综合结论：三维复盘"
    assert "canvas.section_patched" in [e.event_type for e in ctx.parent_bus.events]


async def test_chapter_title_respects_the_revise_scope(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _write_report
    from bowhead_core.canvas_revise import note_revise_scope

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_report(store)
    bus = _Bus()
    note_revise_scope(bus, {ids["ch3_h"], ids["ch3_n"]})
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=bus)
    by_id = {s.id: s for s in await store.list_sections(ids["canvas"])}
    tok = set_current_peer_name("compute")
    try:
        denied = await _write_report(
            {"kind": "narrative", "content": "上半年门店排名", "section_id": ids["ch1_h"], "expected_rev": by_id[ids["ch1_h"]].rev}, ctx
        )
        empty = await _write_report(
            {"kind": "narrative", "content": "  #  \n", "section_id": ids["ch3_h"], "expected_rev": by_id[ids["ch3_h"]].rev}, ctx
        )
    finally:
        reset_current_peer_name(tok)
    assert denied.get("error") == "out_of_revise_scope" and empty.get("error") == "empty_title"
    assert {s.id: s for s in await store.list_sections(ids["canvas"])}[ids["ch1_h"]].payload["text"] == "2026年门店营业额排名"


async def test_chapter_title_write_takes_only_the_title_line(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review P1：写手把整章正文投到章标题的 id 上——只存首行还回 ok，它会以为正文已改。现在拒并指回正文段；
    带 [N] / 锚 / 链接的标题也拒（标题不进 bake，会原样渲出来）。写成功记进本轮「写成功」账（review P2）。"""
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _write_report
    from bowhead_core.canvas_revise import other_writer_succeeded

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_report(store)
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=_Bus())
    rev = {s.id: s for s in await store.list_sections(ids["canvas"])}[ids["ch3_h"]].rev
    tok = set_current_peer_name("compute")
    try:
        body = await _write_report(
            {"kind": "narrative", "content": "## 2026年1-6月营业额趋势\n\n上半年 91.17亿元。", "section_id": ids["ch3_h"], "expected_rev": rev}, ctx
        )
        marked = await _write_report({"kind": "narrative", "content": "上半年趋势 [1]", "section_id": ids["ch3_h"], "expected_rev": rev}, ctx)
        ok = await _write_report({"kind": "narrative", "content": "2026年1-6月营业额趋势", "section_id": ids["ch3_h"], "expected_rev": rev}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert body.get("error") == "chapter_title_only" and "narrative" in body["hint"], body
    assert marked.get("error") == "title_has_markup", marked
    assert ok.get("ok") is True
    assert other_writer_succeeded(ctx.parent_bus, "someone_else")


def test_decimal_values_near_year_range_are_still_number_tokens() -> None:
    """review P1：「.」曾被当日期分隔——「2026.5亿元」「1985.3万元」整个被当日期剔掉，残留扫描就看不见它们了。"""
    from bowhead_core.canvas_revise import number_tokens

    assert number_tokens("营收 1985.3 万元，同比 2020.5%，总额 2026.5亿元") == {"1985.3万元", "2020.5%", "2026.5亿元"}
    assert number_tokens("2025-2026 两年累计 91.17亿") == {"91.17亿"}
    assert number_tokens("截至 2026-09 共 2026 万元") == {"2026万元"}
    assert number_tokens("指数 2026.5") == {"2026.5"}  # Unitless decimal: only preserved by unsplit dot.
    assert "30%" in number_tokens("区间 2020-30%")  # Date-like with unit: only unit lookahead preserves.


async def test_custom_agent_cannot_retitle_a_prior_chapter(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """跨轮就地改只给系统写手（与正文同一道闸，tech-design custom-agent-report-pen）：成员建的 agent 改不了上一轮的章标题。"""
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _write_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    ids = await _seed_plan_report(store)
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=_Bus())
    rev = {s.id: s for s in await store.list_sections(ids["canvas"])}[ids["ch3_h"]].rev
    tok = set_current_peer_name("gina")
    try:
        res = await _write_report({"kind": "narrative", "content": "上半年趋势", "section_id": ids["ch3_h"], "expected_rev": rev}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert res.get("error") == "kind_not_editable", res
    assert {s.id: s for s in await store.list_sections(ids["canvas"])}[ids["ch3_h"]].payload["text"] == "2026年月度营业额趋势"


def test_revise_instructions_mention_chapter_titles_and_dates_are_not_number_tokens() -> None:
    from bowhead_core.canvas_revise import canvas_mode_context_line, number_tokens

    line = canvas_mode_context_line("revise", "报告")
    assert "章标题" in line and "kind=h1" in line
    # 116 1.3.49: "数据截至2026-09-24" split into "09"/"24", falsely called stale in provenance notice.
    assert number_tokens("数据截至2026-09-24，统计区间 2026/1/1 至 2026年6月30日") == set()
    assert number_tokens("6月营业额 12.55 亿元，环比 -3.10%，40 家门店") == {"12.55亿元", "-3.10%", "40"}


async def test_remove_chart_is_open_to_team_agents_with_the_switch_on(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # User decision 2026-10-08: an Agent may hide charts in the user's session just as the user can.
    ids = await _seed_prior_report(store)
    done = await _call_remove(monkeypatch, store, "my-team-agent", {"section_ids": [ids["trend_chart"]], "reason": "removed"})
    assert done["removed"] == [ids["trend_chart"]]

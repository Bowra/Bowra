"""report-engine Phase-2 WP-B — read_report / write_report handler behavior.

Enforcement is in the handler (the schema layer doesn't validate args on this
stack), so these tests assert the handler actually rejects/translates: owner,
expected_rev, figure-id, continuation-guard, error dicts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.agents._lead_context import (
    reset_current_peer_name,
    set_current_peer_name,
)
from bowhead_core.agents.report_tools import _read_report, _write_report

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def ctx(tmp_path: Path):
    store = CanvasStore(tmp_path / "t.db")
    await store.initialize()
    # ensure the canvas exists for session s1 up front
    await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return SimpleNamespace(
        canvas_store=store,
        session_id="s1",
        turn_id="t1",
        workspace_id="w",
        user_id="u",
        parent_bus=SimpleNamespace(),
    )


async def test_answer_plan_rejects_report_creation_before_any_canvas_write(ctx) -> None:
    ctx.session_id = "answer_session"
    ctx.parent_bus._d6_plan_deliverable_kind = "answer"
    result = await _write_report({"content": "## Refund policy\nNet revenue is 1080 USD."}, ctx)
    assert result.get("error") == "plan_answer_only"
    assert "subagent_return" in result["hint"]
    assert await ctx.canvas_store.get_canvas_by_session(ctx.session_id) is None


async def test_answer_plan_preserves_existing_report_on_rejected_update(ctx) -> None:
    original = await _write_report({"content": "Original report."}, ctx)
    ctx.turn_id = "answer_turn"
    ctx.parent_bus._d6_plan_deliverable_kind = "answer"
    result = await _write_report({"content": "Unexpected update.", "section_id": original["section_id"], "expected_rev": original["rev"]}, ctx)
    assert result.get("error") == "plan_answer_only"
    report = await _read_report({"include_full": True}, ctx)
    section = next(s for s in report["sections"] if s["section_id"] == original["section_id"])
    assert section["content"] == "Original report."


@pytest.mark.parametrize("plan_delivery", [None, "report"])
async def test_report_writes_remain_available_outside_answer_plans(ctx, plan_delivery) -> None:
    ctx.parent_bus._d6_plan_deliverable_kind = plan_delivery
    result = await _write_report({"content": "Requested report."}, ctx)
    assert result["ok"] is True
    report = await _read_report({"include_full": True}, ctx)
    assert any(s["content"] == "Requested report." for s in report["sections"])


def _as(peer: str):
    """context manager-ish: set the current peer (the would-be owner)."""
    return set_current_peer_name(peer)


async def test_write_new_then_read(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "# Hi\n\nbody"}, ctx)
        assert r["ok"] and r["owner"] == "compute" and r["rev"] == 1
        sid = r["section_id"]
    finally:
        reset_current_peer_name(tok)

    rr = await _read_report({}, ctx)
    assert rr["ok"]
    line = next(o for o in rr["outline"] if o["section_id"] == sid)
    assert line["owner"] == "compute" and line["kind"] == "narrative"


async def test_update_by_owner_with_rev(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
        sid, rev = r["section_id"], r["rev"]
        r2 = await _write_report(
            {"kind": "narrative", "content": "b", "section_id": sid, "expected_rev": rev},
            ctx,
        )
        assert r2["ok"] and r2["rev"] == 2
    finally:
        reset_current_peer_name(tok)
    full = await _read_report({"include_full": True}, ctx)
    assert any(s["content"] == "b" for s in full["sections"])


async def test_update_without_rev_is_rejected(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
        # a 2nd-writer-style explicit section_id update with no rev → rev_required
        r2 = await _write_report(
            {"kind": "narrative", "content": "b", "section_id": r["section_id"]},
            ctx,
        )
        assert r2.get("error") == "rev_required"
    finally:
        reset_current_peer_name(tok)


async def test_wrong_rev_conflicts(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
        await _write_report(
            {"kind": "narrative", "content": "b", "section_id": r["section_id"], "expected_rev": 1},
            ctx,
        )
        stale = await _write_report(
            {"kind": "narrative", "content": "c", "section_id": r["section_id"], "expected_rev": 1},
            ctx,
        )
        assert stale.get("error") == "rev_conflict" and stale["current_rev"] == 2
    finally:
        reset_current_peer_name(tok)


async def test_other_writer_cannot_patch(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
        sid, rev = r["section_id"], r["rev"]
    finally:
        reset_current_peer_name(tok)
    tok2 = _as("marketing-analyst")
    try:
        hijack = await _write_report(
            {"kind": "narrative", "content": "evil", "section_id": sid, "expected_rev": rev},
            ctx,
        )
        assert hijack.get("error") == "not_owner" and hijack["owner"] == "compute"
        # canvas-update-turn batch A4: offer recovery (the model does not know its own ID; owner alone is insufficient)
        assert "hint" in hijack and "section_id" in hijack["hint"]
    finally:
        reset_current_peer_name(tok2)


async def test_outline_marks_prior_turn_and_editable(ctx) -> None:
    """批 A4：outline 每段带 prior_turn / editable；superseded 段一律不可改。"""
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "mine"}, ctx)
        mine = r["section_id"]
    finally:
        reset_current_peer_name(tok)
    tok2 = _as("marketing-analyst")
    try:
        r2 = await _write_report({"kind": "narrative", "content": "theirs"}, ctx)
        theirs = r2["section_id"]
        out = await _read_report({}, ctx)
        by_id = {o["section_id"]: o for o in out["outline"]}
        assert by_id[theirs]["editable"] is True and by_id[theirs]["prior_turn"] is False
        assert by_id[mine]["editable"] is False  # Another writer's section in the same turn
        await ctx.canvas_store.mark_sections_superseded(section_ids=[theirs], superseded_by_turn="t1")
        out2 = await _read_report({}, ctx)
        assert next(o for o in out2["outline"] if o["section_id"] == theirs)["editable"] is False
    finally:
        reset_current_peer_name(tok2)


async def test_outline_prior_turn_section_marked_and_not_editable(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """批 A（flag 关）：上一轮别的写手的段 prior_turn=True 且 editable=False（批 B0 flag 开后才会变 True）。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "old"}, ctx)
        old = r["section_id"]
    finally:
        reset_current_peer_name(tok)
    ctx.turn_id = "t2"  # Next turn
    tok2 = _as("marketing-analyst")
    try:
        o = next(x for x in (await _read_report({}, ctx))["outline"] if x["section_id"] == old)
        assert o["prior_turn"] is True and o["editable"] is False
    finally:
        reset_current_peer_name(tok2)


async def test_successful_write_is_recorded_on_bus(ctx) -> None:
    """批 A3：write_report 成功在 turn 级 bus 记账（eager 抑制按这本账判，不按调用存在判）。"""
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
        assert r["ok"] is True
        assert ctx.parent_bus._write_report_ok_writers == {"compute"}
        bad = await _write_report({"kind": "narrative", "content": "b", "section_id": r["section_id"], "expected_rev": 99}, ctx)
        assert bad.get("error") == "rev_conflict"
        assert ctx.parent_bus._write_report_ok_writers == {"compute"}  # Failure adds no writer record
    finally:
        reset_current_peer_name(tok)


async def test_leading_h1_title_is_recorded_on_bus_before_bake_strips_it(ctx) -> None:
    """canvas-elements tech-design-mixed-turn-chrome §2.3 标题兜底：首行 `# 标题` 被 bake 剥掉（既有行为），
    剥之前记到 turn 级 bus；peer 崩在 subagent_return 之前时收尾器拿它当 h1。只记 append 路的第一次。"""
    from bowhead_core.canvas_revise import authored_title

    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "# 2025 全年经营六维分析报告\n\n导语。\n\n## 营业额\n正文"}, ctx)
        assert r["ok"] is True
        assert authored_title(ctx.parent_bus) == "2025 全年经营六维分析报告"
        canvas = await ctx.canvas_store.get_canvas_by_session("s1")
        first = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == r["section_id"])
        assert not first.payload["markdown"].lstrip().startswith("# "), "首行 h1 仍被剥掉，不进正文"
        # Updates leave the record unchanged; a second append does not overwrite it either
        up = await _write_report(
            {"kind": "narrative", "content": "# 改过的标题\n\n新导语", "section_id": r["section_id"], "expected_rev": r["rev"]}, ctx
        )
        assert up["ok"] is True
        assert authored_title(ctx.parent_bus) == "2025 全年经营六维分析报告"
    finally:
        reset_current_peer_name(tok)


async def test_rejected_write_does_not_pin_the_authored_title(ctx) -> None:
    """★★review 第 5 条：标题原来记在 bake **之前**且 first-wins ⇒ 一次被拒的写入就把假标题钉死，
    后面真写成的那次再也盖不掉，文档顶着一个从没落库过的段的标题。只有写成功才记。"""
    from bowhead_core.canvas_revise import authored_title

    tok = _as("compute")
    try:
        seed = await _write_report({"kind": "narrative", "content": "占位"}, ctx)
        assert seed["ok"] is True
        # An update guaranteed to fail (rev conflict), with a title on the first body line
        bad = await _write_report(
            {"kind": "narrative", "content": "# 从没落库的标题\n\n正文", "section_id": seed["section_id"], "expected_rev": 99}, ctx
        )
        assert bad.get("error") == "rev_conflict"
        assert authored_title(ctx.parent_bus) is None, "被拒的写入不该钉住标题"
    finally:
        reset_current_peer_name(tok)

    tok2 = _as("peerB")
    try:
        ok = await _write_report({"kind": "narrative", "content": "# 真正落库的标题\n\n正文"}, ctx)
        assert ok["ok"] is True
        assert authored_title(ctx.parent_bus) == "真正落库的标题"
    finally:
        reset_current_peer_name(tok2)


async def test_no_leading_h1_records_no_title(ctx) -> None:
    """正对照：正文不以 `# ` 开头 ⇒ 不记（收尾器仍宁可不发 h1，不拿用户原话兜底）。"""
    from bowhead_core.canvas_revise import authored_title

    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "导语。\n\n## 营业额\n正文"}, ctx)
        assert r["ok"] is True
        assert authored_title(ctx.parent_bus) is None
    finally:
        reset_current_peer_name(tok)


# ─── canvas-update-turn batch B0: owner becomes a same-turn lock (FEATURE_CANVAS_SECTION_REVISE) ───


async def _seed_prior_turn_section(ctx) -> tuple[str, int]:
    """上一轮（t1）compute 写的一段；随后把 ctx 拨到 t2。"""
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "old"}, ctx)
    finally:
        reset_current_peer_name(tok)
    ctx.turn_id = "t2"
    return r["section_id"], r["rev"]


async def test_cross_turn_patch_allowed_with_flag_and_ledger_records_writer(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    sid, rev = await _seed_prior_turn_section(ctx)
    tok = _as("marketing-analyst")
    try:
        r = await _write_report({"kind": "narrative", "content": "revised", "section_id": sid, "expected_rev": rev}, ctx)
        assert r.get("ok") is True and r["rev"] == rev + 1, r
        canvas = await ctx.canvas_store.get_canvas_by_session("s1")
        sec = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == sid)
        assert sec.payload["markdown"] == "revised" and sec.owner == "compute"  # Section owner stays unchanged (lineage)
        import sqlite3

        con = sqlite3.connect(str(ctx.canvas_store._path))
        row = con.execute(
            "select op, owner, by_turn_id from canvas_revisions where section_id=? order by rowid desc limit 1",
            (sid,),
        ).fetchone()
        con.close()
        assert row == ("patch", "marketing-analyst", "t2"), row  # Ledger records the actual writer, not NULL
    finally:
        reset_current_peer_name(tok)


async def test_cross_turn_patch_rejected_without_flag(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    sid, rev = await _seed_prior_turn_section(ctx)
    tok = _as("marketing-analyst")
    try:
        r = await _write_report({"kind": "narrative", "content": "x", "section_id": sid, "expected_rev": rev}, ctx)
        assert r.get("error") == "not_owner" and "hint" in r
    finally:
        reset_current_peer_name(tok)


async def test_same_turn_other_writer_still_rejected_with_flag(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
    finally:
        reset_current_peer_name(tok)
    tok2 = _as("marketing-analyst")
    try:
        bad = await _write_report({"kind": "narrative", "content": "b", "section_id": r["section_id"], "expected_rev": r["rev"]}, ctx)
        assert bad.get("error") == "not_owner" and "本轮另一位写手" in bad["hint"]
    finally:
        reset_current_peer_name(tok2)


async def test_superseded_section_rejected_and_rev_conflict_kept(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    sid, rev = await _seed_prior_turn_section(ctx)
    tok = _as("marketing-analyst")
    try:
        bad = await _write_report({"kind": "narrative", "content": "x", "section_id": sid, "expected_rev": rev + 5}, ctx)
        assert bad.get("error") == "rev_conflict"
        await ctx.canvas_store.mark_sections_superseded(section_ids=[sid], superseded_by_turn="t2")
        sup = await _write_report({"kind": "narrative", "content": "x", "section_id": sid, "expected_rev": rev}, ctx)
        assert sup.get("error") == "section_superseded"
        o = next(x for x in (await _read_report({}, ctx))["outline"] if x["section_id"] == sid)
        assert o["editable"] is False
    finally:
        reset_current_peer_name(tok)


async def test_outline_prior_turn_editable_with_flag(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    sid, _ = await _seed_prior_turn_section(ctx)
    tok = _as("marketing-analyst")
    try:
        o = next(x for x in (await _read_report({}, ctx))["outline"] if x["section_id"] == sid)
        assert o["prior_turn"] is True and o["editable"] is True
    finally:
        reset_current_peer_name(tok)


# ─── B0 review fixes ──────────────────────────────────────────────────────


async def test_superseded_target_not_rejected_without_flag(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """flag 关：superseded 判定不生效（写路与批 A 逐字节等价）——同 owner 改自己已折叠的段照旧走 owner/rev 路。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "old"}, ctx)
        await ctx.canvas_store.mark_sections_superseded(section_ids=[r["section_id"]], superseded_by_turn="t1")
        again = await _write_report({"kind": "narrative", "content": "x", "section_id": r["section_id"], "expected_rev": r["rev"]}, ctx)
        assert again.get("ok") is True, again
    finally:
        reset_current_peer_name(tok)


async def test_prior_turn_non_narrative_kind_rejected_and_not_editable(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修：上一轮的 h1/kpi_row/图不可就地改（write_report 只写 markdown，patch 上去看不见）；
    outline editable=false，写路返 kind_not_editable 而不是 not_owner。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    h1 = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="h1", payload={"text": "旧标题"}, by_turn_id="t1", owner="sub_prev")
    kpi = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="kpi_row", payload={"cells": []}, by_turn_id="t1", owner="sub_prev")
    ctx.turn_id = "t2"
    tok = _as("marketing-analyst")
    try:
        outline = {x["section_id"]: x for x in (await _read_report({}, ctx))["outline"]}
        assert outline[h1.id]["prior_turn"] is True and outline[h1.id]["editable"] is False
        assert outline[kpi.id]["editable"] is False
        bad = await _write_report({"kind": "narrative", "content": "新标题", "section_id": h1.id, "expected_rev": h1.rev}, ctx)
        assert bad.get("error") == "kind_not_editable" and bad.get("kind") == "h1" and "add_chart" in bad["hint"]
        sec = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == h1.id)
        assert sec.payload == {"text": "旧标题"} and sec.rev == h1.rev  # Unchanged
    finally:
        reset_current_peer_name(tok)


async def test_cross_turn_patch_keeps_style_and_reports_true_owner(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    cav = await ctx.canvas_store.append_section(
        canvas_id=canvas.id, kind="narrative", payload={"markdown": "局限", "style": "caveats"}, by_turn_id="t1", owner="sub_prev"
    )
    ctx.turn_id = "t2"
    tok = _as("marketing-analyst")
    try:
        r = await _write_report({"kind": "narrative", "content": "局限（修订）", "section_id": cav.id, "expected_rev": cav.rev}, ctx)
        assert r.get("ok") is True and r["owner"] == "sub_prev", r  # Return the actual owner, not the current-turn writer
        sec = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == cav.id)
        assert sec.payload["style"] == "caveats" and sec.payload["markdown"] == "局限（修订）"
    finally:
        reset_current_peer_name(tok)


async def test_section_from_another_canvas_is_not_found(ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修（既有漏洞）：别的会话画布里的 section_id 不可改——此前 patch_section 按裸 id 查表，
    owner 为 NULL 的段（图/收尾器 chrome）传过来就改成功。两态都拒。"""
    canvas_other = await ctx.canvas_store.ensure_canvas(session_id="s_other", workspace_id="w", user_id="u")
    victim = await ctx.canvas_store.append_section(canvas_id=canvas_other.id, kind="narrative", payload={"markdown": "victim"}, by_turn_id="t0")
    for flag in (None, "1"):
        if flag is None:
            monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
        else:
            monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", flag)
        tok = _as("evil")
        try:
            r = await _write_report({"kind": "narrative", "content": "pwned", "section_id": victim.id, "expected_rev": victim.rev}, ctx)
            assert r.get("error") == "section_not_found", r
        finally:
            reset_current_peer_name(tok)
    sec = next(s for s in await ctx.canvas_store.list_sections(canvas_other.id) if s.id == victim.id)
    assert sec.payload == {"markdown": "victim"} and sec.rev == victim.rev


async def test_in_txn_superseded_raise_translated_by_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-round review：快照里没折叠、事务里折叠了（TOCTOU）⇒ store 抛 CanvasSectionSupersededError，handler 译成 section_superseded。"""
    from bowhead_core.agents.report_tools import _do_update
    from bowhead_core.canvas_store import CanvasSectionSupersededError

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    sec = SimpleNamespace(id="sec_x", kind="narrative", turn_id="t1", superseded=False, owner="sub_prev", payload={"markdown": "old"})

    class _Store:
        async def list_sections(self, _cid, include_hidden=False):  # noqa: ARG002
            return [sec]

        async def patch_section(self, **kwargs):
            assert kwargs["reject_superseded"] is True and kwargs["canvas_id"] == "cv1" and kwargs["skip_owner_check"] is True
            raise CanvasSectionSupersededError("sec_x")

    tok = _as("marketing-analyst")  # Cross-turn admission checks trust class for the ContextVar peer
    try:
        r = await _do_update(_Store(), "sec_x", {"markdown": "new"}, 1, "marketing-analyst", "t2", canvas_id="cv1")
    finally:
        reset_current_peer_name(tok)
    assert r.get("error") == "section_superseded", r


async def test_cross_turn_patch_opens_canvas_turn_and_emits_patched(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修（SSE 视角）：纯改段的轮可能没有任何 section_added——写路先发 canvas.turn_streaming（FE 整轮锁）。"""
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    sid, rev = await _seed_prior_turn_section(ctx)
    events: list = []

    async def _emit(ev):
        events.append(ev)

    ctx.parent_bus = SimpleNamespace(emit=_emit)
    tok = _as("marketing-analyst")
    try:
        r = await _write_report({"kind": "narrative", "content": "revised", "section_id": sid, "expected_rev": rev}, ctx)
        assert r.get("ok") is True
        types = [getattr(e, "event_type", None) for e in events]
        assert types[-2:] == ["canvas.turn_streaming", "canvas.section_patched"], types
    finally:
        reset_current_peer_name(tok)


async def test_continuation_guard_routes_to_update_not_second_append(ctx) -> None:
    """Same writer, no section_id, already wrote a section this turn → UPDATE it
    (handler-side), not a 2nd narrative append (the forgot-id F12 reborn)."""
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": "first"}, ctx)
        r2 = await _write_report({"kind": "narrative", "content": "first + more"}, ctx)  # no section_id
        assert r2["ok"] and r2["section_id"] == r1["section_id"] and r2["rev"] == 2
    finally:
        reset_current_peer_name(tok)
    rr = await _read_report({}, ctx)
    narr = [o for o in rr["outline"] if o["kind"] == "narrative"]
    assert len(narr) == 1  # exactly one narrative section, not two


async def test_unknown_figure_ref_at_line_end_is_stripped_not_rejected(ctx) -> None:
    """**行尾**无效锚：bake 静默剥除、正文照落（delivery-surface M-B 把卫兵下沉进笔
    后的有意行为，`report_tools` 那句「bake 已剥非法 figure 锚 → 本闸正常永不触发」
    说的就是这条路）。

    ⚠ 本测试此前断言旧的硬拒行为，M-B 之后恒红——红着的测试等于没有测试，
    所以下面那条「行中形仍硬拒」的正对照必须一起在，否则闸真被拆了也没人知道。"""
    tok = _as("compute")
    try:
        r = await _write_report(
            {"kind": "narrative", "content": "see [figure:sec_made_up]"},
            ctx,
        )
        assert r.get("ok") is True and not r.get("error")
        sid = r["section_id"]
    finally:
        reset_current_peer_name(tok)
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    sec = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == sid)
    md = (sec.payload or {}).get("markdown", "")
    # Crucial: writing requires anchors to be fully stripped; otherwise fake IDs enter prose and FE renders broken figure slots
    assert "sec_made_up" not in md and "[figure:" not in md


async def test_unknown_figure_ref_mid_line_is_stripped_not_rejected(ctx) -> None:
    """A mid-line invalid anchor is stripped like the line-end form, not rejected.

    The invariant the previous version of this test defended -- a fake id must
    never reach the stored prose -- still holds; only the enforcement moved from
    "reject the whole report" to "strip the token". 116 `t_6846eb7b274a` is why:
    one misplaced anchor sank three consecutive `write_report` calls and the
    turn ended up composed without its writer."""
    tok = _as("compute")
    try:
        r = await _write_report(
            {"kind": "narrative", "content": "见图 [figure:sec_made_up] 后续正文。"},
            ctx,
        )
        assert r.get("ok") is True and not r.get("error")
        sid = r["section_id"]
    finally:
        reset_current_peer_name(tok)
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    sec = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == sid)
    md = (sec.payload or {}).get("markdown", "")
    assert "sec_made_up" not in md and "[figure:" not in md
    # the prose on both sides of the anchor survives
    assert "见图" in md and "后续正文" in md


async def test_figure_ref_inside_code_fence_is_not_rejected(ctx) -> None:
    """Brackets inside a fenced block are not references.

    The bake-side stripper has always skipped fences; this gate used not to, so
    a report that merely *showed* `[figure:...]` in a code sample was rejected
    wholesale. Both sides now read fences the same way."""
    content = "正文。\n\n```\n[figure:sec_made_up]\n```\n\n继续。"
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": content}, ctx)
        assert r.get("ok") is True and not r.get("error")
        sid = r["section_id"]
    finally:
        reset_current_peer_name(tok)
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    sec = next(s for s in await ctx.canvas_store.list_sections(canvas.id) if s.id == sid)
    assert "sec_made_up" in (sec.payload or {}).get("markdown", "")


async def test_unknown_figure_ref_rejection_names_the_valid_ids(ctx, monkeypatch) -> None:
    """When the gate does fire, the rejection has to carry the right answer.

    Reachable only when bake itself fails (`write_report.bake_failed`, guards
    degraded), which is exactly when the writer most needs a usable error. The
    old hint pointed at `read_report` -- a tool the writer prompt tells it not
    to call -- and never named a single legal id, so the model reran the same
    bytes three times."""
    import bowhead_core.report_writer_loop as rwl

    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    chart = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "T"}})

    async def _boom(*a, **k):
        raise RuntimeError("bake exploded")

    monkeypatch.setattr(rwl, "bake_report_markdown", _boom)
    tok = _as("compute")
    try:
        r = await _write_report(
            {"kind": "narrative", "content": "见图 [figure:sec_made_up] 后续正文。"},
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert r.get("error") == "unknown_figure_ref"
    assert r["bad_ids"] == ["sec_made_up"]
    assert r["valid_ids"] == [chart.id]
    assert chart.id in r["hint"]
    assert "read_report" not in r["hint"]


async def test_unknown_figure_ref_rejection_says_so_when_there_are_no_figures(ctx, monkeypatch) -> None:
    """Zero figures is the case that actually happened: the writer ran before a
    single chart existed. Saying "there are none, delete the anchors" is the
    only correction available to it."""
    import bowhead_core.report_writer_loop as rwl

    async def _boom(*a, **k):
        raise RuntimeError("bake exploded")

    monkeypatch.setattr(rwl, "bake_report_markdown", _boom)
    tok = _as("compute")
    try:
        r = await _write_report(
            {"kind": "narrative", "content": "见图 [figure:sec_made_up] 后续正文。"},
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert r.get("error") == "unknown_figure_ref" and r["valid_ids"] == []
    assert "没有任何可引用的图表" in r["hint"]


async def test_anchor_to_chat_hidden_figure_promotes_it_under_the_same_id(ctx) -> None:
    """116 `t_6846eb7b274a`: the previous turn drew charts and answered in chat,
    so D6 hid them; "turn the analysis above into a report, with charts" then
    anchored them by their real ids and got `unknown_figure_ref` three times.
    The anchor must be accepted, the figure must come back onto the canvas with
    the same id, and the live client must hear about it."""
    events: list[tuple[str, dict]] = []

    class _Bus:
        async def emit(self, ev):
            events.append((ev.event_type, ev.payload))

    ctx.parent_bus = _Bus()
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    chart = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "营业额月度走势"}})
    await ctx.canvas_store.set_section_surface_target(chart.id, "chat")  # hidden, as D6 leaves it
    assert chart.id not in {s.id for s in await ctx.canvas_store.list_sections(canvas.id)}

    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": f"营业额逐月下滑。\n\n[figure:{chart.id}]\n\n继续。"}, ctx)
    finally:
        reset_current_peer_name(tok)

    assert r.get("ok") is True and not r.get("error")
    visible = {s.id: s for s in await ctx.canvas_store.list_sections(canvas.id)}
    assert chart.id in visible  # same id, back on the canvas
    narr = visible[r["section_id"]]
    assert f"[figure:{chart.id}]" in (narr.payload or {}).get("markdown", "")
    added = [d["section"]["id"] for name, d in events if name == "canvas.section_added"]
    assert chart.id in added  # the live client re-inserts it


async def test_read_report_lists_chat_hidden_figures_as_history(ctx) -> None:
    """`read_report` used to return `figures: []` after a chat turn, so the
    writer redrew the same charts. Hidden figures are listed after the visible
    ones, marked as history; superseded ones are not listed."""
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    visible = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "可见图"}})
    hidden = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "对话里的图"}})
    await ctx.canvas_store.set_section_surface_target(hidden.id, "chat")
    replaced = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "旧版"}})
    await ctx.canvas_store.set_section_surface_target(replaced.id, "chat")
    await ctx.canvas_store.mark_sections_superseded(section_ids=[replaced.id], superseded_by_turn="t0")

    r = await _read_report({}, ctx)
    ids = [f["id"] for f in r["figures"]]
    assert ids == [visible.id, hidden.id]
    hist = r["figures"][1]
    assert hist["fresh"] is False and "无需重画" in hist["note"]


async def test_superseded_figure_is_not_promoted_and_the_rejection_says_why(ctx, monkeypatch) -> None:
    """A figure that was replaced by a newer version stays unreferenceable; when
    the gate does fire it says "replaced", not "unknown"."""
    import bowhead_core.report_writer_loop as rwl

    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    old = await ctx.canvas_store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "old"}})
    await ctx.canvas_store.mark_sections_superseded(section_ids=[old.id], superseded_by_turn="t0")
    await ctx.canvas_store.set_section_surface_target(old.id, "chat")

    async def _boom(*a, **k):
        raise RuntimeError("bake exploded")

    monkeypatch.setattr(rwl, "bake_report_markdown", _boom)
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": f"见图 [figure:{old.id}] 。"}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert r.get("error") == "unknown_figure_ref"
    assert r["replaced_ids"] == [old.id]
    assert "已被新版本替换" in r["hint"]
    assert old.id not in {s.id for s in await ctx.canvas_store.list_sections(canvas.id)}


async def test_real_figure_ref_accepted(ctx) -> None:
    # seed a real chart figure
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    chart = await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"title": "T"}},
    )
    tok = _as("compute")
    try:
        r = await _write_report(
            {"kind": "narrative", "content": f"see [figure:{chart.id}]"},
            ctx,
        )
        assert r["ok"]
    finally:
        reset_current_peer_name(tok)


def test_subagent_id_contextvar_imported_from_correct_root() -> None:
    """Regression for the wrong-relative-import in subagent.py (committed 24442c9f):
    `from ._lead_context import …` resolves to the non-existent bowhead_core root
    (the module lives under bowhead_core.agents) → ModuleNotFoundError on EVERY
    peer spawn. It passed ruff + unit tests + create_app() boot smoke and only
    surfaced on a real turn (subagent._legacy_run). This guards the exact line."""
    import importlib
    import inspect

    import bowhead_core.subagent as sa
    from bowhead_core.agents._lead_context import (  # correct root
        get_current_subagent_id,
        reset_current_subagent_id,
        set_current_subagent_id,
    )

    assert all(callable(f) for f in (get_current_subagent_id, set_current_subagent_id, reset_current_subagent_id))
    # the wrong root must not exist (so the bug can't masquerade as valid)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bowhead_core._lead_context")
    # subagent.py must import the contextvar helpers from the agents subpackage
    src = inspect.getsource(sa)
    assert "from .agents._lead_context import" in src
    assert "from ._lead_context import" not in src  # the bug shape


async def test_non_int_expected_rev_gives_rev_required(ctx) -> None:
    """A non-integer expected_rev (LLM passed a string/garbage) must translate to
    the actionable `rev_required` error, not an opaque ValueError (re-review P0)."""
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "a"}, ctx)
        bad = await _write_report(
            {"kind": "narrative", "content": "b", "section_id": r["section_id"], "expected_rev": "not-a-number"},
            ctx,
        )
        assert bad.get("error") == "rev_required"
    finally:
        reset_current_peer_name(tok)


async def test_continuation_guard_recovers_when_recorded_section_deleted(ctx) -> None:
    """If the writer's recorded section was deleted out from under it, a
    no-section_id continuation must drop the stale mapping and append fresh —
    not dead-end on section_not_found (re-review P1)."""
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": "first"}, ctx)
        await ctx.canvas_store.delete_section(section_id=r1["section_id"])
        r2 = await _write_report({"kind": "narrative", "content": "fresh"}, ctx)  # no id
        assert r2["ok"] and r2["section_id"] != r1["section_id"] and r2["rev"] == 1
    finally:
        reset_current_peer_name(tok)


async def test_continuation_guard_does_not_hijack_different_kind(ctx) -> None:
    """The guard is keyed by (writer, kind): a deliberate NEW section of a
    DIFFERENT kind by the same writer is a fresh append, not an UPDATE of the
    existing narrative (re-review handler-P0 — same-key-only hijack)."""
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": "narr"}, ctx)
        r2 = await _write_report({"kind": "callout", "content": "note"}, ctx)  # diff kind, no id
        assert r2["ok"] and r2["section_id"] != r1["section_id"]
    finally:
        reset_current_peer_name(tok)
    rr = await _read_report({}, ctx)
    kinds = sorted(o["kind"] for o in rr["outline"])
    assert kinds == ["callout", "narrative"]


async def test_read_full_does_not_leak_figure_payload(ctx) -> None:
    """include_full content for a non-narrative (figure) section must be None —
    never the raw payload dict with internal/structural fields (re-review read-P1)."""
    canvas = await ctx.canvas_store.get_canvas_by_session("s1")
    await ctx.canvas_store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"title": "T", "data": [1, 2, 3]}, "secret_internal": "x"},
    )
    tok = _as("compute")
    try:
        await _write_report({"kind": "narrative", "content": "prose body"}, ctx)
    finally:
        reset_current_peer_name(tok)
    full = await _read_report({"include_full": True}, ctx)
    chart = next(s for s in full["sections"] if s["kind"] == "chart")
    narr = next(s for s in full["sections"] if s["kind"] == "narrative")
    assert chart["content"] is None  # no raw payload leak
    assert narr["content"] == "prose body"


class _CapturingBus:
    """Records emitted OrchestratorEvents for emit-contract assertions."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


async def test_write_report_emits_section_added_live(tmp_path: Path) -> None:
    """M1 (report-pipeline-reliability): write_report MUST emit canvas.section_added
    on the live bus so the report NARRATIVE renders during the turn — not only
    after a manual F5 hydrate. Regression guard for the persist-without-emit bug
    (prod t_feae6f38: narrative persisted, 0 turn_events → blank until refresh,
    while charts/kpi/sources — which already emit — rendered live)."""
    store = CanvasStore(tmp_path / "t.db")
    await store.initialize()
    await store.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    bus = _CapturingBus()
    ctx = SimpleNamespace(
        canvas_store=store,
        session_id="s1",
        turn_id="t1",
        workspace_id="w",
        user_id="u",
        parent_bus=bus,
    )
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "# T\n\nbody prose"}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert r["ok"]
    added = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_added"]
    assert len(added) == 1, f"append must emit exactly one canvas.section_added; got {[getattr(e, 'event_type', None) for e in bus.events]}"
    assert added[0].payload["section"]["id"] == r["section_id"]
    assert added[0].payload.get("canvas_id")

    # the update (continuation) path must also emit so a patched narrative
    # re-renders live rather than reverting to stale text until F5.
    tok = _as("compute")
    try:
        r2 = await _write_report(
            {"kind": "narrative", "content": "body prose v2", "section_id": r["section_id"], "expected_rev": r["rev"]},
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert r2["ok"]
    # canvas-comprehensive WP-4 (B3): the UPDATE path now emits canvas.section_patched
    # (was section_added, violating REPORT_WRITING.md contract 1). Append still section_added;
    # update = patched carrying the FULL section as `patch` (late-join upsert safe).
    added2 = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_added"]
    assert len(added2) == 1  # only the append emitted section_added
    patched = [e for e in bus.events if getattr(e, "event_type", None) == "canvas.section_patched"]
    assert len(patched) == 1  # the update emitted section_patched, not a 2nd section_added
    assert patched[0].payload["section_id"] == r["section_id"]
    assert patched[0].payload["patch"]["kind"] == "narrative"  # full section carried in patch


# ─────────────────────────────────────────────────────────────────
# canvas-elements step 2: persist authored prose by chapter (tech-design-chapter-split.md §4)
# ─────────────────────────────────────────────────────────────────

_THREE = "导语一段。\n\n## 品类结构\n\n女装 36.8%。\n\n## 逐月趋势\n\n3 月 -20.2%。\n\n## 结论\n\n女装托底。\n"


async def _body(ctx) -> list[dict]:
    rr = await _read_report({"include_full": True}, ctx)
    return [s for s in rr["sections"] if s["kind"] == "narrative"]


async def test_write_report_lands_lead_plus_chapters(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": _THREE}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert r["ok"] and r["chapters"] == 4 and len(r["section_ids"]) == 4 and r["section_id"] == r["section_ids"][0]
    secs = await _body(ctx)
    assert [s["owner"] for s in secs] == ["compute"] * 4
    assert "".join(s["content"] for s in secs) == _THREE.rstrip("\n")  # Byte preservation (bake strips only trailing newlines)
    rr = await _read_report({}, ctx)
    titles = [o["title"] for o in rr["outline"] if o["kind"] == "narrative"]
    assert titles == ["", "品类结构", "逐月趋势", "结论"]  # Writer chooses sections by title


async def test_write_report_single_section_without_heading_is_unchanged(ctx) -> None:
    tok = _as("compute")
    try:
        r = await _write_report({"kind": "narrative", "content": "只有一段。\n\n**加粗不是标题**\n\n内容。"}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert r["ok"] and "chapters" not in r
    assert len(await _body(ctx)) == 1


async def test_continuation_reconciles_by_chapter_title(ctx) -> None:
    """整篇重投：同标题 patch、新章追加在末尾、旧章不删。"""
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": _THREE}, ctx)
        redo = "新导语。\n\n## 逐月趋势\n\n改写后的趋势。\n\n## 建议\n\n新加的一章。\n"
        r2 = await _write_report({"kind": "narrative", "content": redo}, ctx)  # no section_id
    finally:
        reset_current_peer_name(tok)
    ids = r1["section_ids"]
    assert r2["ok"] and r2["updated"] == [ids[0], ids[2]] and len(r2["appended"]) == 1
    secs = await _body(ctx)
    assert [s["section_id"] for s in secs] == [*ids, r2["appended"][0]]  # New chapter last; preserve old chapters
    assert secs[0]["content"].strip() == "新导语。" and "改写后的趋势" in secs[2]["content"] and "女装 36.8%" in secs[1]["content"]
    assert secs[4]["content"].startswith("## 建议")


async def test_update_with_multi_chapter_content_inserts_after_target(ctx) -> None:
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": _THREE}, ctx)
        ids = r1["section_ids"]
        rr = await _read_report({}, ctx)
        rev = next(o["rev"] for o in rr["outline"] if o["section_id"] == ids[1])
        r2 = await _write_report(
            {"kind": "narrative", "section_id": ids[1], "expected_rev": rev, "content": "## 品类结构\n\n改。\n\n## 品类补充\n\n补。\n"},
            ctx,
        )
    finally:
        reset_current_peer_name(tok)
    assert r2["ok"] and r2["chapters"] == 2 and r2["section_ids"][0] == ids[1]
    secs = await _body(ctx)
    order = [s["section_id"] for s in secs]
    assert order[:2] == [ids[0], ids[1]] and order[2] == r2["section_ids"][1] and order[3:] == [ids[2], ids[3]]
    assert secs[1]["content"].strip() == "## 品类结构\n\n改。" and secs[2]["content"].strip() == "## 品类补充\n\n补。"


async def test_add_chapter_turn_lands_as_titled_chapter_after_last_body(ctx) -> None:
    """dogfood §5.3：新一轮「在末尾加一章」只交一个 `##` ⇒ 落成带标题的章（style=insight），
    插在最后一段正文之后，而不是排在上一轮的 sources / 建议 / caveats 外壳后面。"""
    store = ctx.canvas_store
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": _THREE}, ctx)
    finally:
        reset_current_peer_name(tok)
    canvas = await store.ensure_canvas(session_id=ctx.session_id, workspace_id="ws_t", user_id="u_t")
    for kind, pl in (
        ("sources", {"items": []}),
        ("narrative", {"markdown": "## 建议\n\n1. 做。", "style": "recommendations"}),
        ("narrative", {"markdown": "口径说明。", "style": "caveats"}),
    ):
        await store.append_section(canvas_id=canvas.id, kind=kind, payload=pl, by_turn_id=ctx.turn_id)
    tok = _as("analyst")
    try:
        r2 = await _write_report({"kind": "narrative", "content": "## 五、各品类退款率\n\n退款率 3%。\n"}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert r2["ok"] and r2["chapters"] == 1 and r2["section_ids"] == [r2["section_id"]]
    secs = await store.list_sections(canvas.id)
    new = next(s for s in secs if s.id == r2["section_id"])
    assert new.payload["style"] == "insight" and new.payload["_meta"]["chapter_title"] == "五、各品类退款率"
    order = [s.id for s in secs]
    assert order.index(new.id) == order.index(r1["section_ids"][-1]) + 1  # Immediately after the "结论" chapter
    assert [s.kind for s in secs[order.index(new.id) + 1 :]] == ["sources", "narrative", "narrative"]  # Chrome remains afterward
    # Same-turn update of this chapter: the whole-document path's executive style must not replace its style; preserve chapter title
    tok = _as("analyst")
    try:
        r3 = await _write_report(
            {"kind": "narrative", "section_id": new.id, "expected_rev": new.rev, "content": "## 五、各品类退款率\n\n退款率 4%。\n"}, ctx
        )
    finally:
        reset_current_peer_name(tok)
    assert r3["ok"]
    cur = next(s for s in await store.list_sections(canvas.id) if s.id == new.id)
    assert cur.payload["style"] == "insight" and cur.payload["_meta"]["chapter_title"] == "五、各品类退款率" and "4%" in cur.payload["markdown"]
    # Title line changed (translated to English) ⇒ synchronize chapter title (dogfood §5.6)
    tok = _as("analyst")
    try:
        r4 = await _write_report(
            {"kind": "narrative", "section_id": new.id, "expected_rev": cur.rev, "content": "## 5. Refund Rate\n\nRefund 4%.\n"}, ctx
        )
    finally:
        reset_current_peer_name(tok)
    assert r4["ok"]
    cur = next(s for s in await store.list_sections(canvas.id) if s.id == new.id)
    assert cur.payload["_meta"]["chapter_title"] == "5. Refund Rate"


async def test_after_section_id_places_new_chapter_between_chapters(ctx) -> None:
    """dogfood §5.5：schema 里的 `after_section_id` 此前 handler 从没读过——「在一二章之间插一章」落到末尾。"""
    store = ctx.canvas_store
    tok = _as("compute")
    try:
        r1 = await _write_report({"kind": "narrative", "content": _THREE}, ctx)
    finally:
        reset_current_peer_name(tok)
    ids = r1["section_ids"]  # Intro, category structure, monthly trend, conclusion
    # Three different writers (a second write by the same writer hits the continuation guard, rather than appending)
    tok = _as("analyst")
    try:
        r2 = await _write_report({"kind": "narrative", "content": "## 一点五\n\n插进来。\n", "after_section_id": ids[1]}, ctx)
    finally:
        reset_current_peer_name(tok)
    tok = _as("consultant")
    try:  # Sections without chapter titles count too
        r3 = await _write_report({"kind": "narrative", "content": "补一句。", "after_section_id": ids[2]}, ctx)
    finally:
        reset_current_peer_name(tok)
    tok = _as("reviewer")
    try:  # Unknown ID uses default position and returns a hint
        r4 = await _write_report({"kind": "narrative", "content": "## 猜的\n\n正文。\n", "after_section_id": "sec_nope"}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert r2["ok"] and r3["ok"] and r4["ok"]
    assert "不存在" not in r2["hint"] and "hint" not in r3 and "after_section_id 不存在" in r4["hint"]
    canvas = await store.ensure_canvas(session_id=ctx.session_id, workspace_id="ws_t", user_id="u_t")
    order = [s.id for s in await store.list_sections(canvas.id)]
    assert order.index(r2["section_id"]) == order.index(ids[1]) + 1  # Between chapters one and two
    assert order.index(r3["section_id"]) == order.index(ids[2]) + 1  # After the monthly-trend chapter
    assert order.index(r4["section_id"]) == order.index(ids[3]) + 1  # Default: after the last body section


async def test_cross_turn_relaxation_is_system_manifest_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """跨轮改**别人**上一轮的正文段：系统 manifest 放行，自建 agent 仍 not_owner。

    2026-09-09（docs/sprints/custom-agent-report-pen-2026-09-09/）：`write_report` 对自建
    agent 放开的同时，唯一收窄的就是这一档——跨轮放行的理由是「上一轮写手已退出、没有并发面」，
    那是系统写手之间的约定；自建 agent 是成员级就能创建的，不该继承。判据复用 `_write_guard`
    的 trust class（CCA 不在系统注册表 ⇒ 查不到 ⇒ fail-closed）。
    """
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")

    tok = _as("compute")  # System manifest (trust_class=report-writer) authors the prior-turn section
    try:
        first = await _write_report({"kind": "narrative", "content": "# T1\n\n上一轮的正文"}, ctx)
        assert first["ok"]
        sid = first["section_id"]
    finally:
        reset_current_peer_name(tok)

    ctx.turn_id = "t2"  # Enter next turn

    # ① Custom agent (name absent from system registry): no cross-turn admission; enforce owner
    tok = _as("regional-data-analyst")
    try:
        denied = await _write_report({"kind": "narrative", "content": "改掉别人的", "section_id": sid, "expected_rev": 1}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert denied.get("error") == "not_owner", denied
    assert denied.get("owner") == "compute"

    # ② Positive control: another system manifest retains cross-turn admission (otherwise trust class is not proven to be the cause)
    tok = _as("business-analyst")
    try:
        allowed = await _write_report({"kind": "narrative", "content": "系统写手改得动", "section_id": sid, "expected_rev": 1}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert allowed.get("ok"), allowed

    # ③ Custom agents can still append their own new sections
    tok = _as("regional-data-analyst")
    try:
        mine = await _write_report({"kind": "narrative", "content": "我自己的一段"}, ctx)
    finally:
        reset_current_peer_name(tok)
    assert mine.get("ok") and mine["owner"] == "regional-data-analyst"

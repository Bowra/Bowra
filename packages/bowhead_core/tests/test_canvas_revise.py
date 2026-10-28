"""canvas-update-turn 批 A（2026-09-03）——`canvas_revise` 纯函数 + 三处接线。

权威：docs/sprints/canvas-update-turn-2026-09-03/tech-design.md §1。
- A2：eager 卡片盖 `_meta.composed_by/chapter`；亲笔判据只排除「eager 且非章」；plan 章仍算亲笔。
- A3：write_report 成功由 handler 在 bus 记账；`_psi_skip_final_compose_only` 只在 eager 真渲过时置位；
  末轮 composer 见本轮已有 eager 段即跳过。
- `_meta` 受 patch 保护（子键级合并）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.agents.lead_orchestrator import _peer_authored_narrative_exists
from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX
from bowhead_core.canvas_composer import compose_canvas_from_payload
from bowhead_core.canvas_revise import (
    is_eager_chapter,
    is_eager_composed,
    is_peer_authored_narrative,
    mark_turn_committed_to_v4,
    note_write_report_ok,
    write_report_succeeded,
)
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.report_writer_loop import _maybe_finish_peer_authored_report
from bowhead_core.types import TurnRequest

pytestmark = pytest.mark.unit


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


def _sec(kind: str = "narrative", owner: str | None = "sub_x", meta: dict | None = None):
    payload: dict = {"markdown": "body"}
    if meta is not None:
        payload["_meta"] = meta
    return SimpleNamespace(kind=kind, owner=owner, payload=payload)


# ─── A2 peer-authored criterion ────────────────────────────────


def test_peer_authored_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    assert is_peer_authored_narrative(_sec()) is True  # Persisted by write_report (unmarked).
    assert is_peer_authored_narrative(_sec(meta={"composed_by": "eager", "chapter": False})) is False  # eager nonchapter: chrome rendered.
    assert is_peer_authored_narrative(_sec(meta={"composed_by": "eager", "chapter": True})) is True  # eager plan chapter: finisher adds doc chrome.
    assert is_peer_authored_narrative(_sec(kind="insight_card")) is False
    assert is_peer_authored_narrative(_sec(owner="lead")) is False
    assert is_peer_authored_narrative(_sec(owner=None)) is False
    assert is_eager_composed(_sec(meta={"composed_by": "eager"})) and not is_eager_chapter(_sec(meta={"composed_by": "eager"}))


def test_peer_authored_kill_switch_restores_owner_prefix_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_EAGER_CHROME_DEDUP", "0")
    assert is_peer_authored_narrative(_sec(meta={"composed_by": "eager", "chapter": False})) is True


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "w.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


async def _seed(store: CanvasStore, *, meta: dict | None, turn: str = "t1") -> None:
    cv = await store.get_canvas_by_session("s1")
    payload: dict = {"markdown": "## 章\n正文", "style": "executive"}
    if meta is not None:
        payload["_meta"] = meta
    await store.append_section(canvas_id=cv.id, kind="narrative", payload=payload, by_turn_id=turn, owner="sub_p1")


def _request(turn: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id=turn, text="q", language_context=None)


async def test_finisher_ignores_eager_non_chapter_but_keeps_chapter(store: CanvasStore) -> None:
    """S5b 形状：eager 非章 narrative 不放行收尾器（否则同轮双 h1/双 KPI 带）；plan 章放行。"""
    await _seed(store, meta={"composed_by": "eager", "chapter": False})
    assert (
        await _maybe_finish_peer_authored_report(canvas_store=store, request=_request(), bus=_Bus(), payload={"title": "T"}, tool_invocations=[])
        is None
    )
    assert await _peer_authored_narrative_exists(SimpleNamespace(canvas_store=store), "t1") is False

    await _seed(store, meta={"composed_by": "eager", "chapter": True}, turn="t2")
    kinds = await _maybe_finish_peer_authored_report(
        canvas_store=store, request=_request("t2"), bus=_Bus(), payload={"title": "封面"}, tool_invocations=[]
    )
    assert kinds is not None and "narrative" in kinds and "h1" in kinds
    assert await _peer_authored_narrative_exists(SimpleNamespace(canvas_store=store), "t2") is True


async def test_finisher_still_fires_for_write_report_narrative(store: CanvasStore) -> None:
    await _seed(store, meta=None)  # write_report sections have no eager mark.
    kinds = await _maybe_finish_peer_authored_report(canvas_store=store, request=_request(), bus=_Bus(), payload={"title": "T"}, tool_invocations=[])
    assert kinds is not None and "h1" in kinds


# ─── A2 eager marking + _meta protection + A3 partial-eager guard ──


def _turn_request(turn: str = "t1") -> TurnRequest:
    return TurnRequest(turn_id=turn, session_id="s1", workspace_id="w", user_id="u", member_uid="m", text="q")


def _payload() -> dict:
    return {
        "title": "报告",
        "exec_summary": "整体平稳。",
        "kpis": [{"label": "营收", "value": "1 亿", "sentiment": "neutral"}],
        "insights": [{"title": "集中", "thesis": "CR3 高", "narrative": "头部集中。", "evidence": []}],
    }


async def test_eager_compose_stamps_meta_and_chapter_flag(store: CanvasStore) -> None:
    kinds = await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request(), bus=_Bus(), payload=_payload(), peer_id="sub_p1", emit_lifecycle=False
    )
    assert "h1" in kinds
    cv = await store.get_canvas_by_session("s1")
    secs = [s for s in await store.list_sections(cv.id) if s.kind in ("h1", "narrative", "kpi_row")]
    assert secs and all(is_eager_composed(s) and not is_eager_chapter(s) for s in secs)

    await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request("t2"), bus=_Bus(), payload=_payload(), peer_id="sub_p2", emit_lifecycle=False, chapter_mode=True
    )
    chapter = [s for s in await store.list_sections(cv.id) if s.turn_id == "t2" and s.kind == "narrative"]
    assert chapter and all(is_eager_chapter(s) for s in chapter)


async def test_lead_end_compose_skips_when_eager_already_landed(store: CanvasStore) -> None:
    """eager 中途异常留下部分段时，末轮（emit_lifecycle=True）不再渲整套（幂等键不同会双份）。"""
    await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request(), bus=_Bus(), payload=_payload(), peer_id="sub_p1", emit_lifecycle=False
    )
    cv = await store.get_canvas_by_session("s1")
    before = len(await store.list_sections(cv.id))
    assert before > 0  # Positive control: eager persisted sections for guard to inspect.
    bus = _Bus()  # No _psi_compose_done (eager unfinished).
    kinds = await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request(), bus=bus, payload=_payload(), peer_id=None, emit_lifecycle=True
    )
    assert kinds == []
    assert len(await store.list_sections(cv.id)) == before


async def test_patch_keeps_protected_meta_subkeys(store: CanvasStore) -> None:
    cv = await store.get_canvas_by_session("s1")
    sec = await store.append_section(
        canvas_id=cv.id, kind="narrative", payload={"markdown": "a", "_meta": {"composed_by": "eager", "chapter": False}}, by_turn_id="t1"
    )
    patched = await store.patch_section(
        section_id=sec.id,
        patch={"markdown": "b", "_meta": {"composed_by": "llm", "chapter": True, "note": "x"}},
        expected_rev=1,
        by_turn_id="t2",
        caller=None,
    )
    assert patched is not None
    assert patched.payload["_meta"] == {"composed_by": "eager", "chapter": False, "note": "x"}
    assert patched.payload["markdown"] == "b"


async def test_skip_owner_check_only_bypasses_owner_lock(store: CanvasStore) -> None:
    """批 B0：`skip_owner_check` 只跳 owner 互斥；protected fields 仍剥、revision 行仍记 caller。"""
    import sqlite3

    from bowhead_core.canvas_store import CanvasNotOwnerError

    cv = await store.get_canvas_by_session("s1")
    sec = await store.append_section(canvas_id=cv.id, kind="narrative", payload={"markdown": "a"}, by_turn_id="t1", owner="sub_prev")
    with pytest.raises(CanvasNotOwnerError):
        await store.patch_section(section_id=sec.id, patch={"markdown": "b"}, expected_rev=1, by_turn_id="t2", caller="sub_now")
    patched = await store.patch_section(
        section_id=sec.id,
        patch={"markdown": "b", "owner": "sub_evil", "kind": "h1", "rev": 99},
        expected_rev=1,
        by_turn_id="t2",
        caller="sub_now",
        skip_owner_check=True,
    )
    assert patched is not None and patched.kind == "narrative" and patched.owner == "sub_prev" and patched.rev == 2
    assert "owner" not in patched.payload and "rev" not in patched.payload
    con = sqlite3.connect(str(store._path))
    row = con.execute("select op, owner, by_turn_id from canvas_revisions where section_id=? order by rowid desc limit 1", (sec.id,)).fetchone()
    con.close()
    assert row == ("patch", "sub_now", "t2")


# ─── A3 ledger and V4 mark ──────────────────────────────────────


def test_write_report_succeeded_prefers_bus_ledger_then_results() -> None:
    bus = SimpleNamespace()
    assert write_report_succeeded(bus, "sub_a", [SimpleNamespace(tool_name="write_report", result={"error": "not_owner", "owner": "sub_z"})]) is False
    assert write_report_succeeded(bus, "sub_a", [SimpleNamespace(tool_name="write_report", result={"ok": True, "section_id": "sec_1"})]) is True
    note_write_report_ok(bus, "sub_a")
    assert write_report_succeeded(bus, "sub_a", []) is True  # Bus ledger if parallel snapshot lacks results.
    assert write_report_succeeded(bus, "sub_b", []) is False
    assert write_report_succeeded(None, "sub_a", None) is False


def test_mark_turn_committed_to_v4_only_skips_when_eager_composed() -> None:
    bus = SimpleNamespace()
    mark_turn_committed_to_v4(bus)
    assert bus._turn_committed_to_v4 is True
    assert not getattr(bus, "_psi_skip_final_compose_only", False)  # No eager rendering: the card path remains available.
    bus2 = SimpleNamespace(_psi_compose_done=True)
    mark_turn_committed_to_v4(bus2)
    assert bus2._psi_skip_final_compose_only is True


# ─── Batch B0: revision entry / terminal / append-chapter modes ──


async def _seed_prior_report(store: CanvasStore) -> tuple[str, str]:
    """t1：一份带 h1 的报告，正文由 sub_prev 亲笔。返回 (canvas_id, narrative_id)。"""
    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="h1", payload={"text": "旧报告"}, by_turn_id="t1")
    n = await store.append_section(
        canvas_id=cv.id, kind="narrative", payload={"markdown": "## 旧正文", "style": "executive"}, by_turn_id="t1", owner="sub_prev"
    )
    return cv.id, n.id


async def test_revise_turn_takes_revise_finisher_and_emits_no_chrome(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """本轮 patch 了上一轮 narrative、没 append h1 ⇒ 修订收尾器：不发 h1/kpi_row，挂 _revised_section_ids。"""
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    cv_id, nid = await _seed_prior_report(store)
    await store.patch_section(
        section_id=nid, patch={"markdown": "## 新口径正文"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True
    )
    bus = _Bus()
    kinds = await compose_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={
            "title": "旧报告",
            "kpis": [{"label": "营收", "value": "1 亿"}],
            "sources": [{"label": "BI", "tool": "bi_query", "artifact_id": "art_1"}],
            "caveats": [FABRICATION_CAVEAT_PREFIX + "1.2 亿"],  # Guard-generated caveat ⇒ provenance notice.
        },
        router=None,
    )
    assert "narrative" in kinds and "narrative_revised" not in kinds
    secs = await store.list_sections(cv_id)
    assert sum(1 for s in secs if s.kind == "h1") == 1  # No second h1.
    assert not any(s.kind == "kpi_row" for s in secs)
    assert bus._revised_section_ids == [nid] and bus._md_finisher_done is True
    # Review test-confidence fix: sources + provenance notices actually persist (narrative in kinds alone missed removal of both).
    assert "sources" in kinds and "provenance_caveats" in kinds
    t2 = await store.list_sections_appended_by_turn("t2")
    assert any(s.kind == "sources" for s in t2)
    assert any(s.kind == "narrative" and s.payload.get("style") == "caveats" and "1.2 亿" in s.payload.get("markdown", "") for s in t2)


async def test_revise_finisher_skipped_in_plan_execution_turn(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修（plan 视角）：plan 执行轮不走修订收尾器——否则重跑步就地改了上一轮的章，
    本轮又没 append h1 时，plan 文档的封面（亲笔收尾器补）会被它吞掉。"""
    from bowhead_core.report_writer_loop import _revise_finish_if_applicable

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    _cv_id, nid = await _seed_prior_report(store)
    await store.patch_section(section_id=nid, patch={"markdown": "重跑"}, expected_rev=1, by_turn_id="t2", caller="sub_ps_3", skip_owner_check=True)
    bus = _Bus()
    bus._d6_is_plan_execution = True
    assert await _revise_finish_if_applicable(canvas_store=store, request=_request("t2"), bus=bus, payload={}, tool_invocations=[]) is None
    assert not getattr(bus, "_md_finisher_done", False)
    bus2 = _Bus()  # Positive control: nonplan turn matches.
    assert await _revise_finish_if_applicable(canvas_store=store, request=_request("t2"), bus=bus2, payload={}, tool_invocations=[]) is not None


async def test_revise_entry_not_taken_when_new_document(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.report_writer_loop import _revise_finish_if_applicable

    cv_id, nid = await _seed_prior_report(store)
    await store.patch_section(section_id=nid, patch={"markdown": "x"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True)
    await store.append_section(canvas_id=cv_id, kind="h1", payload={"text": "新报告"}, by_turn_id="t2")  # New document created this turn.
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    assert await _revise_finish_if_applicable(canvas_store=store, request=_request("t2"), bus=_Bus(), payload={}, tool_invocations=[]) is None


async def test_revise_entry_not_taken_when_flag_off(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """flag 关：同样的修订形状走 compose_report 原路（本轮亲笔 append ⇒ 亲笔收尾器发 h1），不登记修订段。"""
    from bowhead_core.report_writer_loop import compose_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    cv_id, nid = await _seed_prior_report(store)
    await store.patch_section(section_id=nid, patch={"markdown": "x"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True)
    await store.append_section(
        canvas_id=cv_id, kind="narrative", payload={"markdown": "## 另写一段", "style": "executive"}, by_turn_id="t2", owner="sub_now"
    )
    bus = _Bus()
    kinds = await compose_report(canvas_store=store, request=_request("t2"), bus=bus, payload={"title": "T"}, router=None)
    assert "h1" in kinds and not getattr(bus, "_revised_section_ids", None)
    assert sum(1 for s in await store.list_sections(cv_id) if s.kind == "h1") == 2  # Batch A: new report (D6 handles folding).


async def test_terminal_decision_counts_revised_prior_narrative(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """纯措辞修订（零 append 段）终态必须是 canvas，不能判 chat 撤回「报告已更新」。"""
    from bowhead_core.agents import lead_orchestrator as lo

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    _cv_id, nid = await _seed_prior_report(store)
    await store.patch_section(
        section_id=nid, patch={"markdown": "措辞收紧"}, expected_rev=1, by_turn_id="t2", caller="sub_now", skip_owner_check=True
    )

    class _DecisionBus(_Bus):
        _d6_canvas_store = store
        _d6_turn_id = "t2"
        _d6_session_id = "s1"
        _d6_artifact_store = None
        _revised_section_ids = [nid]

    bus = _DecisionBus()
    assert await lo._resolve_terminal_response_kind(bus, "t2", "chat") == "canvas"
    assert bus._terminal_surface_decision["sections"] == []  # Union for decision only, never RC-8/fold/hide.
    # Review fix: absent registered revision finisher, use canvas_revisions ledger; a turn editing sections cannot be chat and lose pointer.
    bus_ledger = _DecisionBus()
    bus_ledger._revised_section_ids = []
    assert await lo._resolve_terminal_response_kind(bus_ledger, "t2", "chat") == "canvas"
    # Control: no current-turn patch in ledger (t3 changed nothing) ⇒ chat.
    bus_plain = _DecisionBus()
    bus_plain._revised_section_ids = []
    bus_plain._d6_turn_id = "t3"
    assert await lo._resolve_terminal_response_kind(bus_plain, "t3", "chat") == "chat"
    # Plan execution excludes union (its documents have separate binding decision).
    bus_plan = _DecisionBus()
    bus_plan._d6_is_plan_execution = True
    assert await lo._resolve_terminal_response_kind(bus_plan, "t2", "chat") == "chat"


@pytest.mark.parametrize("mode", ["append", "revise"])
async def test_append_and_revise_mode_finisher_skips_header_and_kpi(store: CanvasStore, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """append 档与 revise 档（compute 违令没 patch 而是新 append 整篇）都不发 h1/KPI 带——review 修前 revise 档没测。"""
    from bowhead_core.report_writer_loop import _maybe_finish_peer_authored_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    cv_id, _ = await _seed_prior_report(store)
    await store.append_section(
        canvas_id=cv_id, kind="narrative", payload={"markdown": "## 新一章", "style": "executive"}, by_turn_id="t2", owner="sub_now"
    )
    bus = _Bus()
    bus._lead_canvas_mode = mode
    kinds = await _maybe_finish_peer_authored_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"title": "旧报告", "kpis": [{"label": "营收", "value": "1 亿"}]},
        tool_invocations=[],
    )
    assert kinds is not None and "h1" not in kinds and "kpi_row" not in kinds
    assert sum(1 for s in await store.list_sections(cv_id) if s.kind == "h1") == 1


async def test_plan_execution_turn_keeps_cover_even_with_mode_on_bus(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修（plan 视角）：同轮先派过 append 再落进 plan 执行，档位还挂在 bus 上——plan 文档的封面不能被跳掉。"""
    from bowhead_core.report_writer_loop import _maybe_finish_peer_authored_report

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    cv_id, _ = await _seed_prior_report(store)
    await store.append_section(
        canvas_id=cv_id, kind="narrative", payload={"markdown": "## 第一章", "style": "executive"}, by_turn_id="t2", owner="sub_ps_1"
    )
    bus = _Bus()
    bus._lead_canvas_mode = "append"
    bus._d6_is_plan_execution = True
    kinds = await _maybe_finish_peer_authored_report(
        canvas_store=store,
        request=_request("t2"),
        bus=bus,
        payload={"title": "plan 文档", "kpis": [{"label": "营收", "value": "1 亿"}]},
        tool_invocations=[],
    )
    assert kinds is not None and "h1" in kinds


async def test_patch_section_canvas_scope_and_in_txn_superseded_reject(store: CanvasStore) -> None:
    """review 修：canvas_id 作用域（别的画布的 id ⇒ None）；reject_superseded 在事务里拒（快照判定有 TOCTOU 窗）。"""
    from bowhead_core.canvas_store import CanvasSectionSupersededError

    cv_id, nid = await _seed_prior_report(store)
    other = await store.ensure_canvas(session_id="s_other", workspace_id="w", user_id="u")
    assert await store.patch_section(section_id=nid, patch={"markdown": "x"}, canvas_id=other.id, skip_owner_check=True) is None
    ok = await store.patch_section(section_id=nid, patch={"markdown": "y"}, canvas_id=cv_id, skip_owner_check=True)
    assert ok is not None and ok.payload["markdown"] == "y"
    await store.mark_sections_superseded(section_ids=[nid], superseded_by_turn="t2")
    with pytest.raises(CanvasSectionSupersededError):
        await store.patch_section(section_id=nid, patch={"markdown": "z"}, skip_owner_check=True, reject_superseded=True)
    assert (await store.patch_section(section_id=nid, patch={"markdown": "z"}, skip_owner_check=True)) is not None  # Default permits (legacy).


async def test_bake_cross_turn_does_not_mint_this_turn_citations() -> None:
    """review 修：[N] 是 turn 作用域编号，跨轮改段不铸本轮的 [N]（读侧会按旧轮来源解析）；[cite:N] 剥掉。"""
    from bowhead_core.report_writer_loop import bake_report_markdown
    from bowhead_core.sources_resolver import ensure_turn_sources_registry

    bus = _Bus()
    bus._reg_rehydrated = True
    reg = ensure_turn_sources_registry(bus)
    reg.ingest_payload_sources([{"label": "BI", "tool": "bi_query", "artifact_id": "art_1"}], [])
    assert reg.size() == 1
    ctx = SimpleNamespace(parent_bus=bus, turn_id="t2", _report_bake_invocations=[], _cites_are_source_numbers=True)
    same, _ = await bake_report_markdown("营收增长 [cite:1]。", ctx=ctx, is_update=True)
    assert "[1]" in same and "[cite:1]" not in same  # Same-turn update: normal resolution.
    cross, _ = await bake_report_markdown("营收增长 [cite:1]。", ctx=ctx, is_update=True, cross_turn=True)
    assert "[1]" not in cross and "[cite:" not in cross
    kept, _ = await bake_report_markdown("旧文 [2] 保留。", ctx=ctx, is_update=True, cross_turn=True)
    assert "[2]" in kept  # Prior body [N] citations remain valid; retain.


async def test_card_path_in_revise_mode_lands_chapter_at_tail_without_promote(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-round review P1：卡片路在 revise/append 档落的章不能被 N=1 promote-to-top 提到旧报告标题下——按 append 序留在尾部。"""
    from bowhead_core.canvas_revise import strip_document_chrome_payload

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    cv_id, _ = await _seed_prior_report(store)
    old_max_seq = max(s.seq for s in await store.list_sections(cv_id))
    bus = _Bus()
    bus._lead_canvas_mode = "revise"
    kinds = await compose_canvas_from_payload(
        canvas_store=store,
        request=_turn_request("t2"),
        bus=bus,
        payload=strip_document_chrome_payload(_payload(), bus),
        peer_id="sub_p2",
        emit_lifecycle=False,
        chapter_mode=True,
    )
    assert "h1" not in kinds and "kpi_row" not in kinds
    secs = await store.list_sections(cv_id)
    new = [s for s in secs if s.turn_id == "t2"]
    assert new and all(s.seq > old_max_seq for s in new), [(s.kind, s.seq, s.turn_id) for s in secs]
    assert [s.kind for s in secs][:2] == ["h1", "narrative"]  # Prior report beginning not displaced.
    # Positive control: new_report (no mode) still promotes new sections first.
    bus2 = _Bus()
    await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request("t3"), bus=bus2, payload=_payload(), peer_id="sub_p3", emit_lifecycle=False
    )
    assert (await store.list_sections(cv_id))[0].turn_id == "t3"


async def test_write_report_cross_turn_unverified_numbers_tristate(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix-round review：跨轮改段的 unverified_numbers——零 tool 数据 ⇒ 不写键（旧警示留）；有 tool 数据 ⇒ 写，
    旧正文里已有的数字不重标，全验证通过的 [] 照写（真验掉了）。"""
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.report_tools import _write_report
    from bowhead_core.types import ToolInvocation

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    # 2026-09-09: cross-turn owner bypass restricted to system manifests (custom agents do not inherit); criterion
    # maps ContextVar peer name → trust class. Previously test left peer unset (cross-turn permission independent of identity);
    # now set actual writer as in production, or it tests rejection without identity rather than intended behavior.
    tok = set_current_peer_name("compute")
    cv = await store.get_canvas_by_session("s1")
    old = await store.append_section(
        canvas_id=cv.id,
        kind="narrative",
        payload={"markdown": "去年营收 9.9 亿。", "style": "executive", "unverified_numbers": ["9.9 亿"]},
        by_turn_id="t1",
        owner="sub_prev",
    )
    ctx = SimpleNamespace(canvas_store=store, session_id="s1", turn_id="t2", workspace_id="w", user_id="u", parent_bus=_Bus())
    # ① No tool results: omit key ⇒ retain old warning.
    ctx._report_bake_invocations = []
    r = await _write_report({"kind": "narrative", "content": "去年营收 9.9 亿（措辞收紧）。", "section_id": old.id, "expected_rev": old.rev}, ctx)
    assert r.get("ok") is True, r
    sec = next(s for s in await store.list_sections(cv.id) if s.id == old.id)
    assert sec.payload.get("unverified_numbers") == ["9.9 亿"]
    # ② Tool result supports 3.3: ground new 3.3 亿, do not remark reused 9.9 亿, mark invented 7.7 亿.
    ctx._report_bake_invocations = [ToolInvocation(tool_name="bi_query", arguments={}, result={"rows": [{"v": 3.3}]})]
    r2 = await _write_report(
        {"kind": "narrative", "content": "去年营收 9.9 亿，上半年 3.3 亿，预计 7.7 亿。", "section_id": old.id, "expected_rev": sec.rev}, ctx
    )
    assert r2.get("ok") is True, r2
    sec2 = next(s for s in await store.list_sections(cv.id) if s.id == old.id)
    unv = sec2.payload.get("unverified_numbers") or []
    assert not any("9.9" in x for x in unv) and not any("3.3" in x for x in unv), unv
    assert any("7.7" in x for x in unv), unv
    reset_current_peer_name(tok)


async def test_turn_revised_prior_narratives_reads_ledger(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """116 t_6c1cd1b92dfa：canvas-writer 用 canvas_patch_section 改上一轮正文，payload 薄、零 append ⇒ Lead 末轮
    「无内容不 compose」闸拦住修订收尾器。按账本判「本轮改过上一轮正文」= 有产出；flag 关恒空。"""
    from bowhead_core.canvas_revise import turn_revised_prior_narratives

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    _cv_id, nid = await _seed_prior_report(store)
    assert await turn_revised_prior_narratives(store, "s1", "t2") == []  # Not edited yet.
    await store.patch_section(section_id=nid, patch={"markdown": "收紧"}, expected_rev=1, by_turn_id="t2", caller=None)
    got = await turn_revised_prior_narratives(store, "s1", "t2")
    assert [s.id for s in got] == [nid]
    assert await turn_revised_prior_narratives(store, "s1", "t1") == []  # Appended in own turn is no revision.
    assert await turn_revised_prior_narratives(None, "s1", "t2") == [] and await turn_revised_prior_narratives(store, None, "t2") == []
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    assert await turn_revised_prior_narratives(store, "s1", "t2") == []


# ─── Review fix: pure-function criteria ─────────────────────────


def test_is_prior_turn_section_null_and_unknown_turn_rules() -> None:
    from bowhead_core.canvas_revise import is_cross_turn_editable, is_prior_turn_section

    s_null = SimpleNamespace(kind="narrative", turn_id=None, superseded=False)
    s_t1 = SimpleNamespace(kind="narrative", turn_id="t1", superseded=False)
    assert is_prior_turn_section(s_null, "t2") is True  # Legacy turn_id NULL ⇒ prior turn.
    assert is_prior_turn_section(s_t1, "t2") is True and is_prior_turn_section(s_t1, "t1") is False
    assert is_prior_turn_section(s_t1, None) is False and is_prior_turn_section(s_t1, "") is False  # Unknown current turn ⇒ cannot bypass lock.
    # Cross-turn editable = prior turn + body section + not folded.
    assert is_cross_turn_editable(s_t1, "t2") is True
    assert is_cross_turn_editable(SimpleNamespace(kind="kpi_row", turn_id="t1", superseded=False), "t2") is False
    assert is_cross_turn_editable(SimpleNamespace(kind="h1", turn_id="t1", superseded=False), "t2") is False
    assert is_cross_turn_editable(SimpleNamespace(kind="narrative", turn_id="t1", superseded=True), "t2") is False


def test_find_prior_visible_h1_ignores_superseded_and_same_turn() -> None:
    from bowhead_core.canvas_revise import find_prior_visible_h1

    folded = SimpleNamespace(kind="h1", turn_id="t0", superseded=True, payload={"text": "折了"})
    same = SimpleNamespace(kind="h1", turn_id="t2", superseded=False, payload={"text": "本轮 eager"})
    prior = SimpleNamespace(kind="h1", turn_id="t1", superseded=False, payload={"text": "旧报告"})
    assert find_prior_visible_h1([folded, same], "t2") is None
    assert find_prior_visible_h1([folded, same, prior], "t2") is prior
    assert find_prior_visible_h1([SimpleNamespace(kind="h1", turn_id=None, superseded=False, payload={})], "t2") is not None  # NULL ⇒ prior turn.


async def test_canvas_has_prior_report_matches_handler_predicate(store: CanvasStore) -> None:
    from bowhead_core.canvas_revise import canvas_has_prior_report

    assert await canvas_has_prior_report(store, "s1", "t2") is False  # Empty canvas only.
    cv = await store.get_canvas_by_session("s1")
    await store.append_section(canvas_id=cv.id, kind="chart", payload={"spec": {}}, by_turn_id="t1")
    assert await canvas_has_prior_report(store, "s1", "t2") is False  # Chart alone is no report.
    await _seed_prior_report(store)
    assert await canvas_has_prior_report(store, "s1", "t2") is True
    assert await canvas_has_prior_report(store, "s1", "t1") is False  # Current h1 excluded.
    assert await canvas_has_prior_report(None, "s1", "t2") is False and await canvas_has_prior_report(store, None, "t2") is False


def test_no_document_chrome_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import no_document_chrome, strip_document_chrome_payload

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    assert no_document_chrome(SimpleNamespace(_lead_canvas_mode="revise")) is True
    assert no_document_chrome(SimpleNamespace(_lead_canvas_mode="append")) is True
    assert no_document_chrome(SimpleNamespace(_lead_canvas_mode="new_report")) is False
    assert no_document_chrome(SimpleNamespace()) is False and no_document_chrome(None) is False
    assert no_document_chrome(SimpleNamespace(_lead_canvas_mode="revise", _d6_is_plan_execution=True)) is False
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    assert no_document_chrome(SimpleNamespace(_lead_canvas_mode="revise")) is False
    assert strip_document_chrome_payload({"title": "T", "kpis": [1]}) == {"kpis": [1]} and strip_document_chrome_payload(None) == {}
    # append: retain chapter KPIs; revise: no KPI row, including card path inside old report.
    assert strip_document_chrome_payload({"title": "T", "kpis": [1]}, SimpleNamespace(_lead_canvas_mode="append")) == {"kpis": [1]}
    assert strip_document_chrome_payload(
        {"title": "T", "kpis": [1], "exec_summary": "已改口径", "insights": []}, SimpleNamespace(_lead_canvas_mode="revise")
    ) == {"insights": []}  # revise: change note excluded from body.
    assert strip_document_chrome_payload({"title": "T", "exec_summary": "章导语"}, SimpleNamespace(_lead_canvas_mode="append")) == {
        "exec_summary": "章导语"
    }


async def test_alpha_fold_skips_when_lead_mode_is_revise_or_append(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """review 修：Lead 说这轮是 revise/append 就不是新立文档——即便有 h1 漏进来也不折旧报告。"""
    from bowhead_core.agents.lead_orchestrator import _alpha_fold_prior_blocks

    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    monkeypatch.setenv("BOWHEAD_CANVAS_LATEST_WINS", "1")
    cv_id, _ = await _seed_prior_report(store)
    await store.append_section(canvas_id=cv_id, kind="h1", payload={"text": "漏进来的 h1"}, by_turn_id="t2")
    this_turn = await store.list_sections_appended_by_turn("t2")
    bus = _Bus()
    bus._lead_canvas_mode = "append"
    assert await _alpha_fold_prior_blocks(store=store, bus=bus, turn_id="t2", this_turn_sections=this_turn) == []
    assert not any(s.superseded for s in await store.list_sections(cv_id))
    bus2 = _Bus()  # Positive control: new_report still folds.
    assert await _alpha_fold_prior_blocks(store=store, bus=bus2, turn_id="t2", this_turn_sections=this_turn) != []


# ─── Mixed turns: document chrome once per turn (canvas-elements tech-design-mixed-turn-chrome) ──


def test_other_writer_succeeded_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """判据只看 turn 级账本：别的 writer 在 ⇒ True；只有自己 / 账本空 / 无 bus ⇒ False；kill switch 关 ⇒ False。"""
    from bowhead_core.canvas_revise import other_writer_succeeded

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    bus = _Bus()
    assert other_writer_succeeded(bus, "sub_me") is False  # Empty ledger.
    note_write_report_ok(bus, "sub_me")
    assert other_writer_succeeded(bus, "sub_me") is False  # Self only (existing total suppression, no mixed turn).
    note_write_report_ok(bus, "sub_other")
    assert other_writer_succeeded(bus, "sub_me") is True
    assert other_writer_succeeded(bus, None) is True  # Unknown peer identity also counts as other author.
    assert other_writer_succeeded(None, "sub_me") is False
    monkeypatch.setenv("BOWHEAD_EAGER_CHROME_DEDUP", "0")
    assert other_writer_succeeded(bus, "sub_me") is False


def test_strip_mixed_turn_payload_keeps_content_drops_document_fields() -> None:
    from bowhead_core.canvas_revise import strip_mixed_turn_payload

    src = {
        "title": "T",
        "exec_summary": "导语",
        "kpis": [{"label": "a", "value": "1"}],
        "insights": [{"title": "x", "narrative": "y"}],
        "recommendations": [{"action": "do"}],
        "caveats": ["c"],
        "sources": [{"label": "s"}],
    }
    out = strip_mixed_turn_payload(src)
    assert set(out) == {"insights", "recommendations", "caveats", "sources"}
    assert src["title"] == "T", "不改入参"
    assert strip_mixed_turn_payload(None) == {}


def test_strip_mixed_turn_payload_does_not_empty_a_thin_payload() -> None:
    """★★review 第 4 条：薄 payload（只有 exec_summary + kpis，没有 insights）如果三个字段全剥，
    composer 拿到的是空壳，这个 peer 的产出**整个静默消失**——收尾器也不会替它渲正文。
    `title` 照剥（混合轮里它是文档标题，渲出来就是第二个 h1），另两个留着当章的正文。"""
    from bowhead_core.canvas_revise import strip_mixed_turn_payload

    thin = {"title": "T", "exec_summary": "本章结论：会员占比 40.86%。", "kpis": [{"label": "会员占比", "value": "40.86%"}]}
    out = strip_mixed_turn_payload(thin)
    assert "title" not in out, "文档标题仍要剥"
    assert out.get("exec_summary") == "本章结论：会员占比 40.86%。", "没有 insights 时不能把正文也剥掉"
    assert out.get("kpis"), "同上"


def test_turn_chrome_present_counts_only_document_level_pieces(monkeypatch: pytest.MonkeyPatch) -> None:
    """eager **非章**整套 / 无主段算；eager 章（plan 腿、混合轮后到的 payload）与亲笔段的 h1 / KPI 不算。"""
    from bowhead_core.canvas_revise import turn_chrome_present

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    eager_full = {"composed_by": "eager", "chapter": False}
    eager_chapter = {"composed_by": "eager", "chapter": True}

    def sec(kind, owner="sub_x", meta=None, style=None, superseded=False):
        payload = {"markdown": "b"}
        if style:
            payload["style"] = style
        if meta is not None:
            payload["_meta"] = meta
        return SimpleNamespace(kind=kind, owner=owner, payload=payload, superseded=superseded)

    # A3 shape: full eager nonchapter chrome.
    a3 = [
        sec("h1", meta=eager_full),
        sec("narrative", meta=eager_full, style="byline"),
        sec("narrative", meta=eager_full, style="executive"),
        sec("kpi_row", meta=eager_full),
        sec("narrative", meta=eager_full, style="insight"),
        sec("narrative", meta=eager_full, style="recommendations"),
        sec("narrative", meta=eager_full, style="caveats"),
        sec("narrative", owner="sub_pen", style="insight"),  # Authored chapter.
    ]
    assert turn_chrome_present(a3) == {"h1", "byline", "kpi_row", "recommendations", "caveats"}
    # Plan legs: eager chapter h1/title and KPI row do not count; finisher still supplies cover.
    assert (
        turn_chrome_present(
            [sec("h1", meta=eager_chapter), sec("kpi_row", meta=eager_chapter), sec("narrative", meta=eager_chapter, style="insight")]
        )
        == set()
    )
    # Authored body / chart / sources are not chrome.
    assert turn_chrome_present([sec("narrative", owner="sub_pen", style="executive"), sec("chart", owner=None), sec("sources", owner=None)]) == set()
    # Unowned sections (Lead final turn / finisher) count; superseded do not.
    assert turn_chrome_present([sec("h1", owner=None), sec("recommendations", owner=None)]) == {"h1", "recommendations"}
    assert turn_chrome_present([sec("h1", owner=None, superseded=True)]) == set()
    # With doc projection off, meta / eyebrow count as byline.
    assert turn_chrome_present([sec("meta", meta=eager_full), sec("eyebrow", meta=eager_full)]) == {"byline"}
    monkeypatch.setenv("BOWHEAD_EAGER_CHROME_DEDUP", "0")
    assert turn_chrome_present(a3) == set()


def test_authored_title_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.canvas_revise import authored_title, note_authored_title

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    bus = _Bus()
    assert authored_title(bus) is None
    note_authored_title(bus, "  ")
    assert authored_title(bus) is None
    note_authored_title(bus, "2025 全年经营六维分析报告")
    note_authored_title(bus, "后来的另一个标题")  # Record only first.
    assert authored_title(bus) == "2025 全年经营六维分析报告"
    assert authored_title(None) is None
    monkeypatch.setenv("BOWHEAD_EAGER_CHROME_DEDUP", "0")
    assert authored_title(bus) is None

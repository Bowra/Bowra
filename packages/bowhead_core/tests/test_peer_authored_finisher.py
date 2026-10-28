"""delivery-surface M-D —— peer 已自行文时的报告收尾器。

peer（M-C 发笔后）用 write_report 落了 narrative（owner=sub_*，服务端盖章）→
compose_report 不再让写手代笔重写，只确定性补发 header/meta/Sources（零 LLM）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.analysis_payload import PLAN_GAP_CAVEAT_PREFIX as _GAP
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.report_writer_loop import compose_report

pytestmark = pytest.mark.asyncio


def _is_body_narrative(s) -> bool:
    """正文 narrative（排除文档形投影的署名行 style=byline——它是 chrome，不是正文）。"""
    return s.kind == "narrative" and (getattr(s, "payload", None) or {}).get("style") != "byline"


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


class _ExplodingRouter:
    """M-D 铁断言：peer 已行文的轮次**零** LLM 调用——被调即炸。"""

    async def invoke(self, agent_type, req, **kw):
        raise AssertionError("peer 已自行文，写手/编辑不该再被调用")


def _request() -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", workspace_id="w", user_id="u", turn_id="t1", text="q")


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "w.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


async def _seed_peer_narrative(store: CanvasStore, turn_id: str = "t1") -> None:
    canvas = await store.get_canvas_by_session("s1")
    await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "## 合规风险\n本轮由律师 agent 亲笔……", "style": "executive"},
        by_turn_id=turn_id,
        owner="sub_lawyer001",
    )


async def test_peer_authored_skips_scribe_and_finishes(store: CanvasStore) -> None:
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "合规巡检", "sources": [{"label": "合同库", "tool": "kb_search", "artifact_id": "art_k1"}]},
        peer_payloads=[],
        router=_ExplodingRouter(),
    )
    # Peer narrative counts toward kinds (report-generated status uses actual content), plus deterministic header/sources.
    assert "narrative" in kinds
    assert "h1" in kinds
    assert "sources" in kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    assert any(s.kind == "h1" for s in secs)
    assert any(s.kind == "sources" for s in secs)
    # Exactly one narrative: no ghostwriting ran.
    assert len([s for s in secs if _is_body_narrative(s)]) == 1


async def test_finisher_double_fire_guarded(store: CanvasStore) -> None:
    await _seed_peer_narrative(store)
    bus = _Bus()
    first = await compose_report(canvas_store=store, request=_request(), bus=bus, payload={"title": "T"}, router=_ExplodingRouter())
    assert first
    second = await compose_report(canvas_store=store, request=_request(), bus=bus, payload={"title": "T"}, router=_ExplodingRouter())
    assert second == []  # Bus flag prevents double emission.


async def test_finisher_emits_kpi_band(store: CanvasStore) -> None:
    """review #4: 亲笔路原来漏发确定性 KPI 带（单代笔路有）——finisher 补上。"""
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T", "kpis": [{"label": "营收", "value": "1.2亿", "delta": "+8%", "sentiment": "good"}]},
        router=_ExplodingRouter(),
    )
    assert "kpi_row" in kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    band = [s for s in secs if s.kind == "kpi_row"]
    assert len(band) == 1
    cell = band[0].payload["cells"][0]
    assert cell["lbl"] == "营收"
    assert cell["sub"].startswith("▲")  # Preserve delta direction semantics (shared _emit_kpi_band extraction).


async def test_finisher_emits_gap_caveats(store: CanvasStore) -> None:
    """review #4（FIX-2 奇偶）：plan 缺腿的 gap caveats 在 leg 结束后才由 Lead
    追加进 payload——行文 peer 写正文时看不见，finisher 必须确定性落段。"""
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T", "caveats": [_GAP + "步骤「按品牌统计退款率」未完成，本报告不含该部分"]},
        router=_ExplodingRouter(),
    )
    assert "caveats" in kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    cav = [s for s in secs if _is_body_narrative(s) and (s.payload or {}).get("style") == "caveats"]
    assert len(cav) == 1
    assert "退款率" in cav[0].payload["markdown"]
    assert _GAP not in cav[0].payload["markdown"], "机器标记不该渲给用户看"


async def test_mixed_remainder_rendered_when_default_scribe(store: CanvasStore) -> None:
    """缺省代笔且 eager 没跑（无 _psi_compose_done）：余量确定性渲染（零 LLM，
    ExplodingRouter 铁断言），不丢未动笔 peer 的域。2026-09-06 起默认形态是文档形。"""
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T"},
        peer_payloads=[
            {"subagent_id": "sub_lawyer001", "payload": {"insights": [{"title": "合规", "detail": "已亲笔"}]}},
            {
                "subagent_id": "sub_analyst002",
                "payload": {
                    "insights": [
                        {
                            "title": "营收异动",
                            "thesis": "华东 -12%",
                            "narrative": "华东区域营收环比 -12%，主要受门店闭店影响，需关注 Q3 恢复节奏。",
                            "evidence": [{"kind": "callout", "callout_text": "-12%", "caption": "华东"}],
                        }
                    ]
                },
            },
        ],
        router=_ExplodingRouter(),
    )
    # Document-shaped projection default on (2026-09-06): remainder renders deterministically as `## heading` narrative, not insight_card.
    assert "insight_card" not in kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    # Peer-authored body remains unique (remainder narratives lack owner, distinct from authored sections).
    assert len([s for s in secs if _is_body_narrative(s) and str(s.owner or "").startswith("sub_")]) == 1
    assert any(_is_body_narrative(s) and "## 营收异动" in (s.payload or {}).get("markdown", "") for s in secs)


async def test_mixed_remainder_skipped_when_eager_already_composed(store: CanvasStore) -> None:
    """卡片模式下 ψ-E eager 已在 peer return 时渲染过余量（bus._psi_compose_done）
    → finisher 后绝不再写一遍（防双份卡片）。"""
    await _seed_peer_narrative(store)
    bus = _Bus()
    bus._psi_compose_done = True
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=bus,
        payload={"title": "T"},
        peer_payloads=[
            {
                "subagent_id": "sub_analyst002",
                "payload": {
                    "insights": [
                        {
                            "title": "营收异动",
                            "thesis": "华东 -12%",
                            "narrative": "华东区域营收环比 -12%，需关注恢复节奏与门店结构调整的中期影响。",
                            "evidence": [{"kind": "callout", "callout_text": "-12%", "caption": "华东"}],
                        }
                    ]
                },
            },
        ],
        router=_ExplodingRouter(),
    )
    assert "insight_card" not in kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    assert not any(s.kind == "insight_card" for s in secs)


async def test_plan_shape_entries_conservatively_excluded(store: CanvasStore) -> None:
    """plan 路 entry（无 subagent_id）按已覆盖处理——宁可少写不双写；
    代笔一步都不该跑（ExplodingRouter 即铁断言）。"""
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T"},
        peer_payloads=[
            {"profile": "compute", "step_id": "ps_1", "payload": {"insights": [{"title": "x", "detail": "y"}]}},
        ],
        router=_ExplodingRouter(),
    )
    assert kinds.count("narrative") == 1


async def test_short_answer_remainder_not_scribed(store: CanvasStore) -> None:
    """余量里只有短答 payload（delivery_surface=chat）→ 聚合器排掉 → 不代笔。"""
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T"},
        peer_payloads=[
            {
                "subagent_id": "sub_calc003",
                "payload": {"delivery_surface": "chat", "exec_summary": "19×29=551", "kpis": [{"label": "积", "value": "551"}]},
            },
        ],
        router=_ExplodingRouter(),
    )
    assert kinds.count("narrative") == 1


async def test_writer_family_sections_do_not_trigger_finisher(store: CanvasStore) -> None:
    """负对照：写手家族 owner（lead/report-editor/writer-*）不算 peer 行文——
    否则缺省代笔永远跑不起来。"""
    canvas = await store.get_canvas_by_session("s1")
    await store.append_section(
        canvas_id=canvas.id,
        kind="narrative",
        payload={"markdown": "代笔草稿"},
        by_turn_id="t1",
        owner="lead",
    )
    from bowhead_core.report_writer_loop import _maybe_finish_peer_authored_report

    got = await _maybe_finish_peer_authored_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={},
        tool_invocations=None,
    )
    assert got is None


# ─────────────────────────────────────────────────────────────
# Batch 2-b0 (plan-report-assembly self-review R3 P1 prerequisite)
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# Batch 2-c0: document chrome must lead (discovered during batch 1 implementation, absent from original design).
# ─────────────────────────────────────────────────────────────


async def test_finisher_promotes_document_chrome_to_front(store: CanvasStore) -> None:
    """末轮 chrome（eyebrow/h1/meta/kpi_row）必须排在正文之前。

    ★根因订正（2026-08-24，实施 2-c0 时查实）：我一度写成「`_apply_canonical_order`
    跑了但分段判据把 chrome 当成又一份报告」——**错的**。`_apply_canonical_order`
    全仓只有一个调用点，在 `compose_canvas_via_writer_loop`（**代笔路**）；
    亲笔路的收尾器 `_maybe_finish_peer_authored_report` **一次排序都不做**，
    纯 append ⇒ chrome 自然落在末尾（生产 t_a06cfbe66ec8 的 seq 76-80 就是 append 序：
    eyebrow→h1→meta→kpi_row→caveats）。
    """
    await _seed_peer_narrative(store)
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "尽调结论", "eyebrow": "财务尽调", "kpis": [{"label": "营收", "value": "1.2亿"}]},
        router=_ExplodingRouter(),
    )
    canvas = await store.get_canvas_by_session("s1")
    secs = sorted(await store.list_sections(canvas.id), key=lambda x: x.seq)
    kinds = [x.kind for x in secs]
    assert "h1" in kinds and "narrative" in kinds, kinds
    assert kinds.index("h1") < kinds.index("narrative"), f"标题被正文压在后面: {kinds}"
    for hdr in ("eyebrow", "meta", "kpi_row"):
        if hdr in kinds:
            assert kinds.index(hdr) < kinds.index("narrative"), f"{hdr} 应领跑: {kinds}"
    # Header keeps canonical internal order: eyebrow → h1 → meta → kpi_row.
    hdr_seq = [k for k in kinds if k in ("eyebrow", "h1", "meta", "kpi_row")]
    assert hdr_seq == sorted(hdr_seq, key=lambda k: ["eyebrow", "h1", "meta", "kpi_row"].index(k)), hdr_seq


async def test_finisher_keeps_peer_narrative_order_among_themselves(store: CanvasStore) -> None:
    """正对照：chrome 提前不许打乱正文之间的相对序（多章的章序是槽位序定的）。"""
    canvas = await store.get_canvas_by_session("s1")
    for i, owner in enumerate(("sub_a", "sub_b", "sub_c")):
        await store.append_section(canvas_id=canvas.id, kind="narrative", payload={"markdown": f"章{i}"}, by_turn_id="t1", owner=owner)
    await compose_report(canvas_store=store, request=_request(), bus=_Bus(), payload={"title": "T"}, router=_ExplodingRouter())
    secs = sorted(await store.list_sections(canvas.id), key=lambda x: x.seq)
    bodies = [x.payload.get("markdown") for x in secs if _is_body_narrative(x) and x.payload.get("markdown", "").startswith("章")]
    assert bodies == ["章0", "章1", "章2"], bodies


# ─────────────────────────────────────────────────────────────
# Batch 2 review B2-3: **card path** (no peer author) must promote the cover too.
# ─────────────────────────────────────────────────────────────


async def test_card_route_also_promotes_document_chrome(store: CanvasStore) -> None:
    """没有任何腿调 write_report 时走「缺省代笔＝卡片」路，封面同样不能掉到底。

    ★review B2-3 逮到的缺口：`promote_document_chrome` 原本只装在**亲笔收尾器**里。
    卡片路 (`compose_report.scribe_cards`) 直接调 `compose_canvas_from_payload`，
    而批 1-b 在「本轮产出腿 N≥2」时会压掉 promote_to_top ⇒ 末轮 chrome 纯 append 落在最末，
    没有任何东西把它提前。本 sprint 的现场恰好有 2 条腿亲笔（走了收尾器），
    所以第一轮实现没暴露这个洞。
    """
    canvas = await store.get_canvas_by_session("s1")
    # Each leg retains **non-narrative** chapter content, bypassing peer-authored finalizer for the card path;
    # two owners trigger batch 1-b's N>=2 gate, suppressing promote_to_top.
    for owner in ("sub_ps_one", "sub_ps_two"):
        await store.append_section(
            canvas_id=canvas.id,
            kind="insight_card",
            payload={"title": f"{owner} 洞察", "thesis": "t", "narrative": "n" * 40},
            by_turn_id="t1",
            owner=owner,
        )
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "尽调结论与报价建议", "exec_summary": "整体结论……", "kpis": [{"label": "营收", "value": "1.2亿"}]},
        router=_ExplodingRouter(),
    )
    secs = sorted(await store.list_sections(canvas.id), key=lambda x: x.seq)
    kinds = [x.kind for x in secs]
    assert "h1" in kinds, kinds
    assert kinds.index("h1") < kinds.index("insight_card"), f"封面掉到正文后面了: {kinds}"


async def test_chapter_chrome_dropped_but_document_pool_still_rendered(store: CanvasStore) -> None:
    """批 2 的**净收益判据**（review B2-5）：章不渲 chrome 了，文档级那份必须真渲出来。

    否则 2-a 就是净丢内容——建议与数据局限性从「每腿一组」直接变成「零组」，
    正是 self-review R3 警告过的那个更糟的结局。
    这里走**亲笔路**（有腿动过笔），验收尾器把聚合 payload 的池子落成段。
    """
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={
            "title": "尽调结论",
            "eyebrow": "财务尽调",
            "kpis": [{"label": "营收", "value": "1.2亿"}],
            # Document-level pool: recommendations and limitations merged across chapters
            "recommendations": [{"priority": "P0", "action": "锁定退款口径"}],
            "caveats": ["BI 返回单行汇总", "竞品口径不一"],
        },
        router=_ExplodingRouter(),
    )
    # Recommendations / author limitations / attribution all retired (tech-design-document-minimalism §2.2-§2.4);
    # finalizer's only remaining document-level additions are title and KPI band.
    for expect in ("h1", "kpi_row"):
        assert expect in kinds, f"文档级 {expect} 没渲出来: {kinds}"
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    assert len([x for x in secs if x.kind == "h1"]) == 1 and len([x for x in secs if x.kind == "kpi_row"]) == 1


async def test_fabrication_caveat_renders_exactly_once(store: CanvasStore) -> None:
    """★同一条造数守卫 caveat 在一份报告里只能出现一次（116 t_0dd71032d772 复现）。

    守卫把判定追加进 `payload["caveats"]`（analysis_payload:check_fabrication_back_reference），
    而亲笔路收尾器有**两个互不排斥的消费者**：
      1. 通用 caveats 段 —— 把 `payload["caveats"]` 整份渲出来
      2. `provenance_caveats()` —— 再按前缀把同一条挑出来，渲成独立的「数据可信度提示」段
    于是用户在一份报告里读到同一句话两遍。

    ⚠ 去重只能**摘带前缀的那几条**，不能整块丢弃 caveats：通用段里还有写手自己写的
    真·建模局限（本例「本报告基于 2026年7月单月数据…」），那些必须一条不少地留着。
    """
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX

    await _seed_peer_narrative(store)
    fab = FABRICATION_CAVEAT_PREFIX + "KPI 3 的 309, KPI 4 的 0.1536"
    own1 = "本报告基于 2026年7月单月数据，未包含环比/同比趋势"
    own2 = "样本量偏小，结论仅供参考"
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T", "caveats": [own1, fab, own2]},
        router=_ExplodingRouter(),
    )
    assert "provenance_caveats" in kinds, kinds

    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    cav_secs = [s for s in secs if _is_body_narrative(s) and (s.payload or {}).get("style") == "caveats"]
    blob = "\n".join(str((s.payload or {}).get("markdown") or "") for s in cav_secs)

    # ① Fabrication-guard sentence: exactly one occurrence across the canvas.
    assert blob.count(fab) == 1, f"造数 caveat 渲了 {blob.count(fab)} 遍:\n{blob}"
    # ② Author-written limitations returned to the body (§2.3), absent here;
    #    guard text is machine-determined and cannot be authored, so retained. Different treatment is central to this design.
    assert own1 not in blob and own2 not in blob
    # ③ It belongs in the **dedicated** data-trust notice section, not a generic section.
    prov = [s for s in cav_secs if fab in str((s.payload or {}).get("markdown") or "")]
    assert len(prov) == 1
    from bowhead_core.report_chrome_text import chrome_text

    # The request carries no language context → English heading (fallback).
    assert chrome_text("provenance_heading", "en-US") in str(prov[0].payload["markdown"])


async def test_fabrication_caveat_dedup_is_bilingual(store: CanvasStore) -> None:
    """en 轮的守卫指控用的是英文前缀 —— filter 必须双语种认。

    前缀同时是落库识别标记（analysis_payload:1092 注释），老 zh caveat 会永续在库，
    所以两个前缀都得排除，不能只认当前 UI 语言那一个。
    """
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX_EN

    await _seed_peer_narrative(store)
    fab_en = FABRICATION_CAVEAT_PREFIX_EN + "KPI 3: 309"
    own = "Single-month sample; no YoY baseline."
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T", "caveats": [own, fab_en]},
        router=_ExplodingRouter(),
    )
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    blob = "\n".join(
        str((s.payload or {}).get("markdown") or "") for s in secs if _is_body_narrative(s) and (s.payload or {}).get("style") == "caveats"
    )
    assert blob.count(fab_en) == 1, f"英文前缀那条渲了 {blob.count(fab_en)} 遍"
    assert own not in blob


# ── empty-report-library: substantive-content marking at compose time ──────────────────
# ★ Pin the **call site**: pure predicate tests still pass if the wrapper's call is removed.


async def test_compose_marks_content_status_empty_when_nothing_was_produced(store: CanvasStore) -> None:
    """无图、payload 无结构化结论、工具零数据 ⇒ 落库 content_status='empty'。

    ⚠2026-08-26 订正（review §5.10 R-1/R-5）：本 docstring 原本写着「对应 116 实测的
    `cv_816091c634d3`（BI 连续 7 次 download_data failed）」——**那句是错的**。
    实测那张 canvas 有 `recommendations` 段（⇒ 结构化 ⇒ 判据永不判它 empty），
    其 turn `t_9502993bdce1` 有 **15 条 tool_result / 74 个 observed 数字** ⇒ 判 `undecided`。

    **本用例的 `tool_invocations=[]` 在生产上不存在**：116 存量 549 个有 section 落库的轮，
    `has_data=False` 的是 **0** 个。所以这条测的是判据的**边界行为**，不是任何真实形态——
    `empty` 那条腿目前够不着生产输入（详见 §5.10 R-1，两条修法待拍）。
    """
    await _seed_peer_narrative(store)
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "查一下销售", "exec_summary": "BI 连续报错，未获取到任何数据"},
        router=_ExplodingRouter(),
        # Actual 116 shape t_57481411d476: both bi_query calls returned download_data failed.
        tool_invocations=[
            SimpleNamespace(tool_name="bi_query", result={"ok": False, "error": "BIQueryFailedError: download_data failed"}, nested=None),
            SimpleNamespace(tool_name="bi_query", result={"ok": False, "error": "BIQueryFailedError: download_data failed"}, nested=None),
        ],
    )
    canvas = await store.get_canvas_by_session("s1")
    row = await _content_status(store, canvas.id)
    assert row == "empty", row


async def test_compose_marks_content_status_substantive_when_there_is_output(store: CanvasStore) -> None:
    """★正对照（review §5.10 R-6 补）：判据**真的跑过**并落了库。

    没有这条，下面那条「误杀防线」是死的——它断言 `!= "empty"`，而未落库时读到的是
    `NULL`，`NULL != "empty"` 恒真 ⇒ 把整段标记删掉它照样绿（实测：摘掉
    `set_content_status` 那一行，原先只有一条转红，误杀防线那条纹丝不动）。
    """
    await _seed_peer_narrative(store)
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "近30天销售表现", "kpis": [{"label": "营业额", "value": "9.10"}]},
        router=_ExplodingRouter(),
        tool_invocations=[SimpleNamespace(tool_name="bi_query", result={"rows": [{"营业额": 9.10, "利润": 1.57}]}, nested=None)],
    )
    canvas = await store.get_canvas_by_session("s1")
    assert await _content_status(store, canvas.id) == "substantive"


async def test_compose_does_not_mark_prose_report_as_empty(store: CanvasStore) -> None:
    """★误杀防线：散文真报告（工具真返了数、但没渲结构化段）**绝不能**被标 empty。

    对应 `cv_c4c9b12c7586`：9 处引注 + 真数字，却一个 chart/kpi_row/recommendations
    段都没有——散文写手把表/图/建议都写进 narrative 里了。

    ⚠ 这条断言的是「没发生」，靠上面那条正对照证明判据确实在跑（`feedback_assert_absence
    _needs_positive_control`）。它自己被 `NULL` 满足，单独看是死的。
    """
    await _seed_peer_narrative(store)
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "近30天销售表现"},
        router=_ExplodingRouter(),
        tool_invocations=[SimpleNamespace(tool_name="bi_query", result={"rows": [{"营业额": 9.10, "利润": 1.57}]}, nested=None)],
    )
    canvas = await store.get_canvas_by_session("s1")
    row = await _content_status(store, canvas.id)
    assert row != "empty", f"散文真报告被标成 empty 了: {row}"


async def test_compose_marking_never_breaks_the_report(store: CanvasStore) -> None:
    """负对照 + fail-open：标记那段炸了，报告照出、kinds 照返。"""
    await _seed_peer_narrative(store)

    class _BoomStore:
        def __getattr__(self, name):
            return getattr(store, name)

        async def get_canvas_by_session(self, *_a, **_k):
            raise RuntimeError("boom")

    kinds = await compose_report(
        canvas_store=_BoomStore(),
        request=_request(),
        bus=_Bus(),
        payload={"title": "T"},
        router=_ExplodingRouter(),
        tool_invocations=[],
    )
    assert kinds, "标记失败把报告带崩了"


async def _content_status(store: CanvasStore, canvas_id: str):
    import aiosqlite
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(store._path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT content_status FROM canvases WHERE id=?", (canvas_id,)) as cur:
            r = await cur.fetchone()
    return r["content_status"] if r else None


async def _force_hidden(store: CanvasStore, section_id: str) -> None:
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(store._path) as db:
        await db.execute("UPDATE canvas_sections SET hidden_for_chat_mode=1 WHERE id=?", (section_id,))
        await db.commit()


async def test_peer_written_visual_sections_count_toward_the_mark(store: CanvasStore) -> None:
    """★116 dogfood `t_c5c57f197cd7` 逮到的：`kinds` ≠「这一轮画布上多了什么」。

    亲笔路（本 sprint 批 3 之后的主路）里，图/表是 peer 直接调 canvas 工具写的，
    收尾器 `written` 的全部词表是 {eyebrow,h1,meta,kpi_row,narrative,sources,caveats,
    recommendations,provenance_caveats} —— **一个 `_FIGURE_KINDS` 都没有**。
    真机那轮画布上有 `kpi_row` + 2 张 `data_table`，而
    `compose_report.peer_authored_finish` 打的是 `kinds=['narrative','meta','sources']`
    ⇒ `visual_output` 那条规则**结构性打不出来**，判据落 undecided、一个字没记。

    这条钉的是**调用点**：把 peer 写的 `data_table` 放进画布，标记必须变成 substantive。
    """
    await _seed_peer_narrative(store)
    canvas = await store.get_canvas_by_session("s1")
    await store.append_section(
        canvas_id=canvas.id,
        kind="data_table",
        payload={"columns": ["市", "营业额"], "rows": [["杭州", "1.2亿"]]},
        by_turn_id="t1",
        owner="sub_lawyer001",
    )
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "近30天销售表现"},  # No kpis/insights/recommendations
        router=_ExplodingRouter(),
        tool_invocations=[SimpleNamespace(tool_name="bi_query", result={"rows": [{"营业额": 9.10}]}, nested=None)],
    )
    assert await _content_status(store, canvas.id) == "substantive"


async def test_hidden_sections_do_not_count_toward_the_mark(store: CanvasStore) -> None:
    """负对照：口径与报告库一致 —— 隐藏的段不算「这一轮画布上多了什么」。

    没有这条，上面那条会退化成「只要库里有过一张表就算」。
    """
    await _seed_peer_narrative(store)
    canvas = await store.get_canvas_by_session("s1")
    sec = await store.append_section(
        canvas_id=canvas.id,
        kind="chart",
        payload={"spec": {"kind": "bar"}},
        by_turn_id="t1",
        owner="sub_lawyer001",
    )
    await _force_hidden(store, sec.id)
    await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={"title": "T"},
        router=_ExplodingRouter(),
        tool_invocations=[SimpleNamespace(tool_name="bi_query", result={"rows": [{"x": 1}]}, nested=None)],
    )
    assert await _content_status(store, canvas.id) != "substantive"


async def test_finisher_chrome_is_the_composer_projection(store: CanvasStore) -> None:
    """canvas-elements 第二步 §2.6 正对照：亲笔路收尾器的外壳（h1 / 署名 / 建议 / 局限性）与
    composer 路对同一份 payload 的投影**逐字节相同**——两条路只剩一份外壳实现。"""
    from bowhead_core.canvas_doc_projection import document_chrome_sections

    await _seed_peer_narrative(store)
    payload = {
        "title": "尽调结论",
        "eyebrow": "财务尽调",
        "recommendations": [{"priority": "P1", "action": "补齐门店口径"}, {"priority": "P0", "action": "锁定退款口径", "owner": "财务"}],
        "caveats": ["BI 返回单行汇总"],
    }
    kinds = await compose_report(canvas_store=store, request=_request(), bus=_Bus(), payload=payload, router=_ExplodingRouter())
    assert "h1" in kinds, kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    got = {
        (s.kind, (s.payload or {}).get("style") or ""): {k: v for k, v in (s.payload or {}).items() if k != "_meta"} for s in secs if s.kind == "h1"
    }
    head, tail = document_chrome_sections(payload, lang="zh-CN", meta_pills=None)
    want = {(x["kind"], x.get("style") or ""): {k: v for k, v in x.items() if k not in ("kind", "_meta")} for x in head + tail}
    # Attribution / recommendations / author limitations retired (§2.2-§2.4); both paths' only remaining shell, the title, must match byte-for-byte.
    assert got == want


# ─── Mixed turn: render document shell once per turn (canvas-elements tech-design-mixed-turn-chrome §2.2) ────────


def _turn_request(turn: str = "t1"):
    from bowhead_core.types import TurnRequest

    return TurnRequest(turn_id=turn, session_id="s1", workspace_id="w", user_id="u", member_uid="m", text="q")


_MIXED_PAYLOAD = {
    "title": "2025 全年经营六维分析报告",
    "eyebrow": "经营分析",
    "exec_summary": "2025 年全年营业额 165.35 亿元，同比仅 +0.02%。",
    "kpis": [{"label": "营业额", "value": "165.35 亿", "sentiment": "neutral"}],
    "insights": [{"title": "营业额全年微增", "thesis": "Q4 回暖", "narrative": "9-12 月环比修复明显。", "evidence": []}],
    "recommendations": [{"priority": "P0", "action": "抓住 Q4 回暖窗口"}],
    "caveats": ["会员利润无口径"],
}


def _count(secs, kind: str, style: str | None = None) -> int:
    return len([x for x in secs if x.kind == kind and (style is None or (x.payload or {}).get("style") == style)])


async def test_finisher_skips_chrome_pieces_eager_already_rendered(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★116 A3 的形状（payload 先到、亲笔后落）：eager **非章**已渲过标题与 KPI 带，
    再有一段亲笔 ⇒ 收尾器此前会无条件再来一套（h1×2、KPI×2）。现在逐件看画布：本轮已有的不再发。

    建议 / 局限 / 署名在 tech-design-document-minimalism 里已整体退役，所以文档级单例外壳
    只剩标题与 KPI 带这两件。"""
    from bowhead_core.canvas_composer import compose_canvas_from_payload

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    bus = _Bus()
    await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request(), bus=bus, payload=dict(_MIXED_PAYLOAD), peer_id="sub_p2", emit_lifecycle=False
    )
    await _seed_peer_narrative(store)
    kinds = await compose_report(canvas_store=store, request=_request(), bus=bus, payload=dict(_MIXED_PAYLOAD), router=_ExplodingRouter())
    assert kinds is not None and "narrative" in kinds
    for piece in ("h1", "kpi_row"):
        assert piece not in kinds, f"本轮 eager 已渲过 {piece}，收尾器不该再发: {kinds}"
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    assert _count(secs, "h1") == 1
    assert _count(secs, "kpi_row") == 1


async def test_finisher_still_emits_cover_when_only_eager_chapters_exist(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照（plan 腿 / 混合轮后到的 payload）：eager **章**的 h1（章名）与章内 KPI 带
    不算文档级外壳——封面仍由收尾器补，否则 plan 文档会被自己的章挡掉封面。"""
    from bowhead_core.canvas_composer import compose_canvas_from_payload

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    bus = _Bus()
    await compose_canvas_from_payload(
        canvas_store=store,
        request=_turn_request(),
        bus=bus,
        payload=dict(_MIXED_PAYLOAD),
        peer_id="sub_leg1",
        emit_lifecycle=False,
        chapter_mode=True,
    )
    kinds = await compose_report(canvas_store=store, request=_request(), bus=bus, payload=dict(_MIXED_PAYLOAD), router=_ExplodingRouter())
    assert kinds is not None
    for piece in ("h1", "kpi_row"):
        assert piece in kinds, f"章不带外壳，封面 {piece} 必须由收尾器补: {kinds}"
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    assert _count(secs, "h1") == 2  # Chapter h1 + cover h1


async def test_finisher_kill_switch_restores_double_chrome(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """kill switch 关 ⇒ 回到今天（双 h1）——证明上面那条的「不发」确实是这个判据在起作用。"""
    from bowhead_core.canvas_composer import compose_canvas_from_payload

    monkeypatch.setenv("BOWHEAD_EAGER_CHROME_DEDUP", "0")
    bus = _Bus()
    await compose_canvas_from_payload(
        canvas_store=store, request=_turn_request(), bus=bus, payload=dict(_MIXED_PAYLOAD), peer_id="sub_p2", emit_lifecycle=False
    )
    await _seed_peer_narrative(store)
    kinds = await compose_report(canvas_store=store, request=_request(), bus=bus, payload=dict(_MIXED_PAYLOAD), router=_ExplodingRouter())
    assert kinds is not None and "h1" in kinds
    canvas = await store.get_canvas_by_session("s1")
    assert _count(await store.list_sections(canvas.id), "h1") == 2


async def test_finisher_uses_authored_title_when_payload_has_none(store: CanvasStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """标题兜底（§2.3）：peer 崩在 subagent_return 之前 ⇒ payload 空、没有 title；亲笔首行 `# 标题` 记在 bus 上
    ⇒ 收尾器拿它当 h1。正对照：payload 有 title 时 payload 优先。"""
    from bowhead_core.canvas_revise import note_authored_title

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    await _seed_peer_narrative(store)
    bus = _Bus()
    note_authored_title(bus, "2025 全年经营六维分析报告")
    kinds = await compose_report(canvas_store=store, request=_request(), bus=bus, payload={}, router=_ExplodingRouter())
    assert kinds is not None and "h1" in kinds, kinds
    canvas = await store.get_canvas_by_session("s1")
    h1 = [x for x in await store.list_sections(canvas.id) if x.kind == "h1"]
    assert len(h1) == 1 and h1[0].payload["text"] == "2025 全年经营六维分析报告"

    bus2 = _Bus()
    note_authored_title(bus2, "亲笔标题")
    store2 = CanvasStore(tmp_path / "w2.db")
    await store2.initialize()
    await store2.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    await _seed_peer_narrative(store2)
    await compose_report(canvas_store=store2, request=_request(), bus=bus2, payload={"title": "payload 标题"}, router=_ExplodingRouter())
    cv2 = await store2.get_canvas_by_session("s1")
    h1b = [x for x in await store2.list_sections(cv2.id) if x.kind == "h1"]
    assert len(h1b) == 1 and h1b[0].payload["text"] == "payload 标题"


async def test_finisher_without_any_title_emits_no_h1(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：payload 无 title 且 bus 没记过亲笔标题 ⇒ 仍然宁可不发 h1（不拿用户原话兜底）。"""
    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    await _seed_peer_narrative(store)
    kinds = await compose_report(canvas_store=store, request=_request(), bus=_Bus(), payload={}, router=_ExplodingRouter())
    assert kinds is not None and "h1" not in kinds


async def test_system_caveats_survive_but_author_caveats_do_not(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """★★局限的三分（tech-design-document-minimalism §2.3）在同一份报告里同时成立：

    - 作者自己写的建模局限 → **不渲**，交回正文（它是内容）
    - 守卫的溯源指控 → **渲**（机器判定，模型自证其罪不会写）
    - plan 缺腿提示 → **渲**，且机器标记要剥掉（作者写正文时那些腿还没结束，写不出来）

    把 §2.3 的任一半改回去这条必红。"""
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    await _seed_peer_narrative(store)
    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={
            "caveats": [
                "样本量偏小，结论仅供参考",
                FABRICATION_CAVEAT_PREFIX + "KPI 3 未找到工具来源",
                _GAP + "「compute」这步执行失败，本报告未包含其分析结果。",
            ]
        },
        router=_ExplodingRouter(),
    )
    assert kinds is not None
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    blob = "\n".join((x.payload or {}).get("markdown") or "" for x in secs if (x.payload or {}).get("style") == "caveats")
    assert "KPI 3 未找到工具来源" in blob, "守卫的指控不能丢"
    assert "这步执行失败" in blob, "plan 缺腿提示不能丢（FIX-2 / 2026-06-25 生产事故）"
    assert _GAP not in blob, "机器标记不该渲给用户看"
    assert "样本量偏小" not in blob, "作者自己写的局限已交回正文"


async def test_byline_present_without_h1_still_emits_the_title(store: CanvasStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """★review 第 3 条：eager 在 `payload.title` 为空时只发 meta（记作 byline）不发 h1。
    合起来判 `{h1,byline} & present` 会整块跳过头部 ⇒ 整份报告一个标题都没有。按件跳才对。"""
    from bowhead_core.canvas_composer import compose_canvas_from_payload

    monkeypatch.delenv("BOWHEAD_EAGER_CHROME_DEDUP", raising=False)
    bus = _Bus()
    await compose_canvas_from_payload(
        canvas_store=store,
        request=_turn_request(),
        bus=bus,
        payload={"eyebrow": "经营分析", "exec_summary": "导语。", "insights": [{"title": "a", "thesis": "b", "narrative": "c", "evidence": []}]},
        peer_id="sub_p2",
        emit_lifecycle=False,
    )
    canvas = await store.get_canvas_by_session("s1")
    assert _count(await store.list_sections(canvas.id), "h1") == 0, "前提：eager 没有 title 就不发 h1"
    await _seed_peer_narrative(store)
    kinds = await compose_report(canvas_store=store, request=_request(), bus=bus, payload={"title": "2025 年经营回顾"}, router=_ExplodingRouter())
    assert kinds is not None and "h1" in kinds, f"标题被整块跳掉了: {kinds}"
    h1 = [x for x in await store.list_sections(canvas.id) if x.kind == "h1"]
    assert len(h1) == 1 and h1[0].payload["text"] == "2025 年经营回顾"

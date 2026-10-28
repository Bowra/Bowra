"""Sprint ν1.5 — RefAllowlist tests.

Covers:
- add() idempotent stable simple-ID
- resolve() simple → real OR passthrough
- to_prompt_xml() format + grouping by kind
- translate_payload() evidence ref fields + inline markers
- translate_marker() [num:V:U:src:N] + [ref:N] forms
- refresh_from_stores() pulls real IDs (with mocks)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from bowhead_core._ref_allowlist import RefAllowlist


@dataclass
class _Inv:
    call_id: str = ""
    tool_name: str = ""
    result: Any = None
    nested: list[_Inv] = field(default_factory=list)


@dataclass
class _Art:
    id: str
    filename: str = ""
    source: str = ""
    created_at: str = ""


# ─── add() / resolve() ───


def test_add_assigns_stable_simple_id() -> None:
    al = RefAllowlist()
    sid_chart = al.add("chart", "sec_a3f9b1")
    sid_data = al.add("data", "art_e1d2c3")
    sid_doc = al.add("doc", "chunk_abc123")
    assert sid_chart == 1
    assert sid_data == 2
    assert sid_doc == 3


def test_add_idempotent_returns_same_simple_id() -> None:
    al = RefAllowlist()
    a = al.add("chart", "sec_a")
    b = al.add("chart", "sec_a")
    assert a == b


def test_add_different_kinds_distinct_ids() -> None:
    al = RefAllowlist()
    c = al.add("chart", "sec_x")
    d = al.add("data", "sec_x")  # Same real_id, different kind: still allocate distinct simple ids.
    assert c != d


def test_resolve_simple_int_to_real() -> None:
    al = RefAllowlist()
    al.add("chart", "sec_real")
    assert al.resolve("1") == "sec_real"


def test_resolve_unknown_int_returns_passthrough() -> None:
    al = RefAllowlist()
    assert al.resolve("999") == "999"  # not in allowlist, passthrough


def test_resolve_real_id_passthrough() -> None:
    al = RefAllowlist()
    al.add("chart", "sec_real")
    # Peer uses real ID rather than simple → passthrough (ν1 validation still catches it).
    assert al.resolve("sec_real") == "sec_real"


# ─── to_prompt_xml ───


def test_to_prompt_xml_empty() -> None:
    assert RefAllowlist().to_prompt_xml() == ""


def test_to_prompt_xml_groups_by_kind() -> None:
    al = RefAllowlist()
    al.add("chart", "sec_a")
    al.add("data", "art_a", filename="report.csv", source="bi", origin="今年 各省 营业额")
    al.add("data", "art_legacy", filename="legacy.csv", source="bi")  # Legacy: no origin
    al.add("data", "art_u", filename="我的报价单.xlsx", source="upload")
    al.add("doc", "chunk_x", file_name="财务.pdf", page=3)
    xml = al.to_prompt_xml()
    assert "<available_refs>" in xml
    assert "<chart" in xml
    assert "<data" in xml
    # Batch 2d: machine-produced data no longer exposes original filenames (another path-like string), but **remove
    # only when origin replaces it**; otherwise it becomes fully anonymous, worse than keeping the filename.
    assert 'file="report.csv"' not in xml, "有 origin 顶上 ⇒ 摘掉文件名"
    assert 'file="legacy.csv"' in xml, "无 origin ⇒ 必须留，否则该条无从识别"
    assert 'file="我的报价单.xlsx"' in xml, "用户上传的文件名是用户的锚点（拍板 10）"
    assert 'source="bi"' in xml
    assert "<doc" in xml
    assert "财务.pdf" in xml
    assert 'page="3"' in xml
    assert "<allowlist_rules>" in xml


def test_allowlist_rules_teach_only_listed_ids() -> None:
    """规则块不得出现「用清单外的真实 ID」这类例外指令。

    artifact-addressing 调研（2026-08-19）：曾有一条「数字源 [num:V:U:src:<id>]: <id> 用
    tool 真实返 call_id」，是本块唯一的例外。116 全量实测 217 条 subagent_return 引用违规里
    **48 条是把 call_id 写进 KB 引用字段** —— 模型把这条例外泛化到了别的字段上。
    同时它与 subagent.py 里「任何 [num:...] 标记都会被 bake 成纯文本」直接矛盾（gen2 已随
    citation-audit M1-0 退役）。此闸防它被加回来。
    """
    al = RefAllowlist()
    al.add("chart", "sec_a")
    al.add("data", "art_a", filename="report.csv", source="bi")
    al.add("doc", "chunk_x", file_name="财务.pdf", page=3)
    al.add("tl", "call_00_abcdef", tool="bi_query")
    rules = al.to_prompt_xml().split("<allowlist_rules>")[1]

    assert "[num:" not in rules, "gen2 [num:] 标记已退役，不该再教给 peer"
    assert "call_id" not in rules, "规则块不得指示 peer 使用清单外的 call_id"
    # Negative control: both required rules remain (otherwise emptying the block could fool this assertion).
    assert "[ref:<id>]" in rules, "KB 引用是活路径（translate_marker 会翻成真 chunk id），不能顺手删掉"
    assert "chart_section_id" in rules


def test_prompt_xml_never_exposes_tool_call_ids() -> None:
    """`tl` kind（call_id）进了 allowlist 但**不该**出现在给 peer 的清单里。

    kind_order 本就不含 tl；此闸防它被加进去 —— 一旦暴露，call_id 就成了模型眼里
    「看起来也像引用」的第六个命名空间。
    """
    al = RefAllowlist()
    al.add("data", "art_a", filename="report.csv", source="bi")
    al.add("tl", "call_00_deadbeef", tool="bi_query")
    xml = al.to_prompt_xml()
    assert "call_00_deadbeef" not in xml


def test_to_prompt_xml_renders_design_ref() -> None:
    """multimodal-V2A: editable design sections surface as <design id=N title=.../>
    so cross-turn peers can pass that id to canvas_update_design_text/flatten."""
    al = RefAllowlist()
    al.add("chart", "sec_chart")
    sid = al.add("design", "sec_design_abc", title="夏日促销海报")
    xml = al.to_prompt_xml()
    assert f'<design kind="design" id="{sid}"' in xml
    assert 'title="夏日促销海报"' in xml
    # rule tells the peer how to use the design id for edit/flatten
    assert "canvas_update_design_text" in xml
    assert "canvas_flatten_design" in xml
    # and the id round-trips back to the real section id
    assert al.resolve(str(sid)) == "sec_design_abc"
    assert al.kind_of(sid) == "design"


def test_to_prompt_xml_caps_per_kind() -> None:
    al = RefAllowlist()
    for i in range(15):
        al.add("chart", f"sec_{i}")
    xml = al.to_prompt_xml(max_items=5)
    # only 5 chart entries (rest trimmed)
    assert xml.count("<chart") == 5


def test_to_prompt_xml_data_cap_prefers_newest_artifacts() -> None:
    al = RefAllowlist()
    for i in range(8):
        al.add(
            "data",
            f"art_old_{i}",
            filename=f"old_{i:02d}.csv",
            created_at=f"2026-01-{i + 1:02d}T00:00:00+00:00",
        )
    for i in range(3):
        al.add(
            "data",
            f"art_new_{i}",
            filename=f"new_{i:02d}.csv",
            created_at=f"2026-02-{i + 1:02d}T00:00:00+00:00",
        )

    xml = al.to_prompt_xml(max_items=5)

    assert xml.count("<data") == 5

    # Batch 2d removed file= from machine-produced data; this tests **sorting/truncation**, so check simple ids.
    # (Ids follow add() order: art_old_0..7 first, then art_new_00..02.)
    def _tag(real_id: str) -> str:
        return f'id="{al._real_to_simple[("data", real_id)]}"'

    def _pos(real_id: str) -> int:
        return xml.index(_tag(real_id))

    assert _pos("art_new_2") < _pos("art_new_1") < _pos("art_new_0"), "新的排前面"
    for kept in ("art_old_7", "art_old_6"):
        assert _tag(kept) in xml
    assert _tag("art_old_0") not in xml, "最旧的被 cap 掉"
    assert al.resolve(str(al._real_to_simple[("data", "art_old_0")])) == "art_old_0"


# ─── translate_payload — evidence fields ───


def test_translate_payload_chart_section_id_simple_int() -> None:
    al = RefAllowlist()
    al.add("chart", "sec_real")
    payload = {
        "insights": [
            {
                "evidence": [{"kind": "chart", "chart_section_id": "1", "caption": "X"}],
            }
        ],
    }
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "sec_real"


def test_translate_payload_table_artifact_id() -> None:
    al = RefAllowlist()
    al.add("data", "art_real_data")
    payload = {
        "insights": [
            {
                "evidence": [{"kind": "table", "table_artifact_id": "1", "caption": "Data"}],
            }
        ],
    }
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["table_artifact_id"] == "art_real_data"


def test_translate_payload_computation_artifact_id() -> None:
    al = RefAllowlist()
    al.add("data", "art_compute")
    payload = {
        "insights": [
            {
                "evidence": [{"kind": "computation", "computation_artifact_id": "1"}],
            }
        ],
    }
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["computation_artifact_id"] == "art_compute"


def test_translate_payload_passes_through_unknown_int() -> None:
    """Unknown simple int → passthrough so ν1 validate catches as hallucinate."""
    al = RefAllowlist()
    al.add("chart", "sec_real")  # only id 1
    payload = {
        "insights": [
            {
                "evidence": [{"kind": "chart", "chart_section_id": "99"}],
            }
        ],
    }
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "99"


def test_translate_payload_passes_through_real_id() -> None:
    """Peer 直接用 real ID → passthrough."""
    al = RefAllowlist()
    payload = {"insights": [{"evidence": [{"kind": "chart", "chart_section_id": "sec_real"}]}]}
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "sec_real"


# ─── translate_marker ───


def test_translate_marker_num_simple_src() -> None:
    al = RefAllowlist()
    al.add("tl", "tl_xyz123")
    out = al.translate_marker("营收 [num:13.9:亿元:src:1] 元")
    assert "tl_xyz123" in out
    assert ":src:1]" not in out


def test_translate_marker_ref_simple() -> None:
    al = RefAllowlist()
    al.add("doc", "chunk_real")
    out = al.translate_marker("见 [ref:1] 文档")
    assert "[ref:chunk_real]" in out


def test_translate_marker_with_highlight() -> None:
    al = RefAllowlist()
    al.add("tl", "tl_real")
    out = al.translate_marker("[num:1.5:倍:src:1#highlight=去年增长]")
    assert "src:tl_real#highlight=去年增长" in out


def test_translate_marker_real_id_passthrough() -> None:
    al = RefAllowlist()
    out = al.translate_marker("[num:1.5:倍:src:tl_existing] [ref:chunk_x]")
    assert "tl_existing" in out
    assert "chunk_x" in out


def test_translate_marker_empty() -> None:
    al = RefAllowlist()
    assert al.translate_marker("") == ""
    assert al.translate_marker("plain text 13.2%") == "plain text 13.2%"


def test_translate_payload_inline_in_narrative_walks_full_tree() -> None:
    al = RefAllowlist()
    al.add("doc", "chunk_doc1")
    al.add("tl", "tl_call1")
    payload = {
        "exec_summary": "见 [ref:1]",
        "insights": [
            {
                "title": "T",
                "narrative": "[num:1.5:倍:src:2] 数字",
                "thesis": "Thesis",
                "evidence": [{"kind": "callout", "callout_text": "见 [ref:1]"}],
            }
        ],
        "recommendations": [
            {
                "rationale": "[num:0.5:percent:src:2]",
                "action": "Do X",
                "expected_impact": "...",
            }
        ],
        "kpis": [{"value": "[num:1:src:2]", "delta": "[num:1:src:2]"}],
        "caveats": ["数据 [ref:1]"],
    }
    al.translate_payload(payload)
    assert "chunk_doc1" in payload["exec_summary"]
    assert "tl_call1" in payload["insights"][0]["narrative"]
    assert "chunk_doc1" in payload["insights"][0]["evidence"][0]["callout_text"]
    assert "tl_call1" in payload["recommendations"][0]["rationale"]
    assert "tl_call1" in payload["kpis"][0]["value"]
    assert "chunk_doc1" in payload["caveats"][0]


# ─── refresh_from_stores ───


class _MockCS:
    def __init__(self, by_kind: dict[str, set[str]]) -> None:
        self._b = by_kind

    async def list_section_ids_by_kind(self, _cid: str, *, kind: str) -> set[str]:
        return self._b.get(kind, set())


class _MockAS:
    def __init__(self, arts: list[_Art]) -> None:
        self._a = arts

    async def list_for_session(self, **kwargs):
        return self._a


@pytest.mark.asyncio
async def test_refresh_from_stores_full() -> None:
    al = RefAllowlist()
    cs = _MockCS({"chart": {"sec_b", "sec_a"}, "code_block": {"sec_code"}})
    ars = _MockAS([_Art(id="art_1", filename="r.csv", source="bi")])
    invs = [
        _Inv(
            call_id="tl_kb_1",
            tool_name="kb_search",
            result={"chunks": [{"chunk_id": "chunk_x", "file_name": "doc.pdf", "page": 2}]},
        )
    ]
    await al.refresh_from_stores(
        canvas_store=cs,
        artifact_store=ars,
        session_id="s_1",
        user_id="u_1",
        canvas_id="cv_1",
        tool_invocations=invs,
    )
    # sorted: sec_a < sec_b → sec_a gets id 1
    assert al.resolve("1") == "sec_a"
    assert al.resolve("2") == "sec_b"
    # data
    real_data = next(v for k, v in [("data", "art_1")] if al.resolve(str(al._real_to_simple[(k, v)])) == v)
    assert real_data == "art_1"
    # doc
    chunk_sid = al._real_to_simple[("doc", "chunk_x")]
    assert al.resolve(str(chunk_sid)) == "chunk_x"


@pytest.mark.asyncio
async def test_refresh_from_stores_exposes_image_sections() -> None:
    """feedback-0713 #3: image section 进主动清单（kind="image"）——peer 能看到
    自己 add_image 刚建的 id 并经 chart_section_id 引用；prompt XML 出
    <image .../> 条目 + 引图规则点名 image 列表。"""
    al = RefAllowlist()
    cs = _MockCS({"chart": {"sec_chart"}, "image": {"sec_img"}})
    await al.refresh_from_stores(
        canvas_store=cs,
        artifact_store=_MockAS([]),
        session_id="s_1",
        user_id="u_1",
        canvas_id="cv_1",
        tool_invocations=[],
    )
    img_sid = al._real_to_simple[("image", "sec_img")]
    # simple-id → real image section id (peer's chart_section_id="<simple>" resolves)
    assert al.resolve(str(img_sid)) == "sec_img"
    xml = al.to_prompt_xml()
    assert f'<image kind="image" id="{img_sid}"' in xml
    assert "image 列表" in xml
    # End-to-end: peer fills chart_section_id with image simple id → translate_payload
    # resolves the real id (resolve is kind-independent; restricting translation by kind would fail this assertion).
    payload = {"insights": [{"evidence": [{"kind": "chart", "chart_section_id": str(img_sid), "caption": "毛利率图"}]}]}
    al.translate_payload(payload)
    assert payload["insights"][0]["evidence"][0]["chart_section_id"] == "sec_img"


@pytest.mark.asyncio
async def test_refresh_idempotent_stable_ids_across_calls() -> None:
    """Existing simple-ids preserved on 2nd refresh; new IDs get next."""
    al = RefAllowlist()
    cs1 = _MockCS({"chart": {"sec_a"}})
    ars = _MockAS([])
    await al.refresh_from_stores(
        canvas_store=cs1,
        artifact_store=ars,
        session_id="s",
        user_id="u",
        canvas_id="cv",
        tool_invocations=[],
    )
    sec_a_sid = al._real_to_simple[("chart", "sec_a")]
    # 2nd refresh adds sec_b (new chart). sec_a sid stays unchanged.
    cs2 = _MockCS({"chart": {"sec_a", "sec_b"}})
    await al.refresh_from_stores(
        canvas_store=cs2,
        artifact_store=ars,
        session_id="s",
        user_id="u",
        canvas_id="cv",
        tool_invocations=[],
    )
    assert al._real_to_simple[("chart", "sec_a")] == sec_a_sid  # Unchanged
    assert ("chart", "sec_b") in al._real_to_simple  # Added


# ─── empty-canvas mitigation batch: canvas_id=None (no canvas in this session yet) ───


class _ExplodingCS:
    """canvas 段一旦被调用就炸——用来证明 canvas_id=None 时真的没碰 canvas_store。"""

    async def list_section_ids_by_kind(self, _cid: str, *, kind: str) -> set[str]:
        raise AssertionError(f"canvas 段不该在无 canvas 时被调用 (kind={kind})")


@pytest.mark.asyncio
async def test_refresh_without_canvas_still_pulls_artifacts_and_kb() -> None:
    """canvas_id=None 只跳过 canvas 段；artifact 与 KB/tool 两段必须照常刷。

    这两段只吃 session_id / tool_invocations，与 canvas 无关。整段跳过会让 peer 在
    「还没写过任何内容」的轮次（改动后这是常态）丢掉引用 KB 片段与数据工件的能力。
    """
    al = RefAllowlist()
    invs = [
        _Inv(
            call_id="tl_kb_1",
            tool_name="kb_search",
            result={"chunks": [{"chunk_id": "chunk_x", "file_name": "doc.pdf", "page": 2}]},
        )
    ]
    await al.refresh_from_stores(
        canvas_store=_ExplodingCS(),
        artifact_store=_MockAS([_Art(id="art_1", filename="r.csv", source="bi")]),
        session_id="s_1",
        user_id="u_1",
        canvas_id=None,
        tool_invocations=invs,
    )
    assert ("data", "art_1") in al._real_to_simple
    assert ("doc", "chunk_x") in al._real_to_simple
    assert ("tl", "tl_kb_1") in al._real_to_simple
    # No canvas sections should exist.
    assert not [k for k in al._real_to_simple if k[0] in {"chart", "image", "code", "design"}]


@pytest.mark.asyncio
async def test_refresh_without_canvas_prompt_xml_is_still_usable() -> None:
    """无 canvas 时 prompt 注入仍要含 data/doc，不能因为跳过 canvas 段就整块空掉。"""
    al = RefAllowlist()
    await al.refresh_from_stores(
        canvas_store=_ExplodingCS(),
        artifact_store=_MockAS([_Art(id="art_1", filename="r.csv", source="bi")]),
        session_id="s_1",
        user_id="u_1",
        canvas_id=None,
        tool_invocations=[],
    )
    xml = al.to_prompt_xml()
    assert "art_1" in xml or 'kind="data"' in xml or "<data" in xml


@pytest.mark.asyncio
async def test_refresh_with_canvas_unchanged() -> None:
    """有 canvas 时行为与改动前逐字一致（canvas 段照常刷）。"""
    al = RefAllowlist()
    await al.refresh_from_stores(
        canvas_store=_MockCS({"chart": {"sec_b", "sec_a"}}),
        artifact_store=_MockAS([]),
        session_id="s_1",
        user_id="u_1",
        canvas_id="cv_1",
        tool_invocations=[],
    )
    assert al.resolve("1") == "sec_a"
    assert al.resolve("2") == "sec_b"


# ── List truncation: retain newest, not oldest (chart-list-freshness 2026-09-09) ──


def test_chart_cap_keeps_the_newest_charts_not_the_oldest() -> None:
    """A capped figure allowlist keeps the newest figures available to their creator.

    Oldest-first truncation would hide the current turn's figures from the peer.
    """
    al = RefAllowlist()
    for i in range(12):
        al.add("chart", f"sec_old{i:02d}")
    fresh = [al.add("chart", f"sec_new{i}") for i in range(3)]

    xml = al.to_prompt_xml(max_items=10)
    shown = {int(n) for n in re.findall(r'<chart kind="chart" id="(\d+)"', xml)}

    assert len(shown) == 10
    assert set(fresh) <= shown, f"本轮新图 {fresh} 必须在清单里，实际 {sorted(shown)}"
    assert 1 not in shown, "最旧的那批该被截掉"


def test_image_cap_keeps_the_newest_too() -> None:
    """image 与 chart 同为视觉引用面，同一条规则。"""
    al = RefAllowlist()
    for i in range(12):
        al.add("image", f"sec_img{i:02d}")
    fresh = al.add("image", "sec_img_fresh")
    shown = {int(n) for n in re.findall(r'<image kind="image" id="(\d+)"', al.to_prompt_xml(max_items=5))}
    assert fresh in shown


def test_kept_items_stay_in_ascending_order() -> None:
    """★ 只截尾、**不整体反转**。

    `doc` 的 chunk 在单次 `kb_search` 内部是按相关性排好的；整体反转会把每次检索里
    最不相关的一条顶到最前面。截断保留尾部即可，顺序不动。
    """
    al = RefAllowlist()
    for i in range(12):
        al.add("chart", f"sec_{i:02d}")
    ids = [int(n) for n in re.findall(r'<chart kind="chart" id="(\d+)"', al.to_prompt_xml(max_items=5))]
    assert ids == sorted(ids), ids


def test_kb_chunks_are_not_capped_at_the_generic_limit() -> None:
    """★ 模块 docstring 一直宣称 doc「no truncation」，代码却和别的 kind 一样截到 10。

    `kb_search` 的 top_k 默认 5、上限 10 ⇒ 一轮 3 次检索就超；被截掉的 chunk 又被
    `<allowlist_rules>`「必须从上面 doc 列表选」挡死 ⇒ **永远引不到**。
    """
    al = RefAllowlist()
    for i in range(24):
        al.add("doc", f"chunk_{i:02d}", file_name=f"doc{i}.pdf")
    xml = al.to_prompt_xml(max_items=10)
    assert xml.count("<doc ") == 24


def test_kb_chunk_list_still_has_an_upper_bound() -> None:
    """放宽不等于无上限——20 次检索也不该把 prompt 撑爆。

    ⚠ 期望值**写死 40**，不读 `_DOC_MAX_ITEMS`：拿被测常量当期望，常量改成 10 万也照样
    绿，等于没有断言（[[feedback_gate_credibility]]「判别器同值等于没有」）。
    """
    al = RefAllowlist()
    for i in range(200):
        al.add("doc", f"chunk_{i:03d}")
    assert al.to_prompt_xml(max_items=10).count("<doc ") == 40


# ── List truncation observability (refs-truncation-observability 2026-09-10) ──


def test_render_stats_reports_shown_total_and_newest() -> None:
    """★ `newest_shown` 是「peer 能不能引到自己刚产出的东西」的唯一线上线索。

    `<available_refs>` 没有任何可读面；图多的会话里模型引不到自己刚画的图时，
    没有这一位就只能靠猜。
    """
    al = RefAllowlist()
    for i in range(14):
        al.add("chart", f"sec_{i:02d}")
    al.add("data", "art_only")

    al.to_prompt_xml(max_items=10)
    shown, total, newest = al.last_render_stats["chart"]
    assert (shown, total) == (10, 14)
    assert newest is True, "保留最新的那版必须让最新的图进清单"
    # Untruncated kinds are recorded as usual too: shown == total.
    assert al.last_render_stats["data"] == (1, 1, True)


def test_newest_shown_is_false_when_the_newest_really_is_dropped() -> None:
    """★★ 让指标**真的能取到 False**，否则它恒真＝没有指标。

    突变验证逮到过：把 `newest_shown` 写死 True，原来那版测试一条都不红。
    `data` 按 `created_at` 排序后截断，所以「最后登记的」完全可能因为时间戳老而被丢掉
    —— 这正是该位取 False 的真实场景（生产代码路径，不是手工重算）。
    """
    al = RefAllowlist()
    for i in range(12):
        al.add("data", f"art_new_{i}", created_at=f"2026-09-{i + 1:02d}T00:00:00+00:00")
    # Registered last (largest simple_id), oldest timestamp ⇒ last by created_at order, truncated.
    al.add("data", "art_stale", created_at="2020-01-01T00:00:00+00:00")

    al.to_prompt_xml(max_items=10)
    shown, total, newest = al.last_render_stats["data"]
    assert (shown, total) == (10, 13)
    assert newest is False, "该位必须能取到 False，否则分辨不了任何东西"


# ── choices_of_kind: rejection hints must list choices actually present in the prompt ─────────────


def test_choices_never_advertise_an_id_that_is_not_in_available_refs() -> None:
    """★ 提示列的号必须是 `<available_refs>` 的**子集**。

    另写一套挑选口径的话，模型会被指向一个它在 prompt 里根本看不到的号——
    从一个坑挪到另一个坑（[[feedback_one_concept_many_implementations]]）。
    所以 `choices_of_kind` 复用 `_items_for_prompt`，且 cap 必须 ≤ prompt 的上限。
    """
    al = RefAllowlist()
    for i in range(14):
        al.add("data", f"art_{i:012x}", created_at=f"2026-09-{i + 1:02d}T00:00:00Z", origin=f"q{i}")

    xml = al.to_prompt_xml(max_items=10)
    shown = {int(m) for m in re.findall(r'kind="data" id="(\d+)"', xml)}
    picked = {sid for sid, _real, _label in al.choices_of_kind("data")}

    assert picked, "清单非空时不该挑不出东西"
    assert picked <= shown, f"提示里列了 prompt 没有的号: {picked - shown}"


def test_choices_prefer_the_newest_data_not_the_oldest() -> None:
    """`data` 按 `created_at` 新→旧（沿用 `_items_for_prompt` 的既有口径）。

    挑最旧的那版会把模型指向上几轮的陈数据——它要画的几乎总是刚取的那份。
    """
    al = RefAllowlist()
    old = al.add("data", "art_000000000001", created_at="2026-01-01T00:00:00Z", origin="旧")
    new = al.add("data", "art_000000000002", created_at="2026-09-24T00:00:00Z", origin="新")

    picked = [sid for sid, _real, _label in al.choices_of_kind("data", cap=1)]

    assert picked == [new], f"cap=1 应留最新的 {new}，实得 {picked}（旧的是 {old}）"


def test_choices_carry_the_query_that_produced_the_data() -> None:
    """说明文字取 `origin`（产出它的那句查询），比文件名可辨认——
    模型要在几份数据里选对一份，靠的就是这半句。"""
    al = RefAllowlist()
    al.add("data", "art_000000000001", filename="result_2.csv", origin="2024 年各省销量")

    assert al.choices_of_kind("data") == [(1, "art_000000000001", "2024 年各省销量")]

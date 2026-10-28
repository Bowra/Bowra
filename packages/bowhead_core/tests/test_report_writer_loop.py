"""report-engine Phase-2 WP-C — writer-loop driver (mocked router).

Validates the loop mechanics against a REAL CanvasStore (not the LLM): the writer
calls read_report → write_report, a narrative section is persisted with owner, and
the loop stops after the first successful write. The real-LLM behavior is covered
by local dogfood, not here."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.report_writer_loop import _resolve_cites, _strip_leading_h1
from bowhead_router.types import ToolCall

from bowhead_core import CanvasStore

if TYPE_CHECKING:
    from pathlib import Path


def _is_body_narrative(s) -> bool:
    """正文 narrative（排除文档形投影的署名行 style=byline——它是 chrome，不是正文）。"""
    return s.kind == "narrative" and (getattr(s, "payload", None) or {}).get("style") != "byline"


def test_resolve_cites_keeps_valid_strips_invalid() -> None:
    # n_sources=2: [cite:1]/[cite:2] → [1]/[2]; out-of-range/0 → stripped
    assert _resolve_cites("营收增长[cite:1]，成本[cite:2]。", 2) == "营收增长[1]，成本[2]。"
    assert _resolve_cites("瞎编[cite:9]，零[cite:0]。", 2) == "瞎编，零。"
    assert _resolve_cites("无来源时全删[cite:1]", 0) == "无来源时全删"
    assert _resolve_cites("空格也认 [cite: 2 ]", 3) == "空格也认 [2]"
    # 2026-06-24 dogfood leak: a non-numeric label (LLM cited a source by name) must be
    # STRIPPED, not leaked raw. Also empty + multi-num forms.
    assert _resolve_cites("摘要[cite:总体摘要]在此。", 3) == "摘要在此。"
    assert _resolve_cites("多个[cite:1,2]删掉。", 3) == "多个删掉。"
    assert _resolve_cites("空的[cite:]也删。", 3) == "空的也删。"


def test_strip_invalid_line_end_anchor_keeps_prose_drops_token() -> None:
    """review F4：行尾无效锚剥 token 保正文；有效锚归一成独占行（PM 段首 lift 才认）。"""
    from bowhead_core.report_writer_loop import _strip_invalid_figure_anchors

    md = "营收持续下行，未见明显反弹。[figure:sec_bogus]\n\n结构上看。[figure:sec_ok]"
    cleaned, n = _strip_invalid_figure_anchors(md, {"sec_ok"})
    assert n == 1
    assert "sec_bogus" not in cleaned
    assert "营收持续下行，未见明显反弹。" in cleaned
    # Normalize valid line-end anchors to standalone lines
    assert "\n[figure:sec_ok]" in cleaned
    assert not any("。[figure:" in ln for ln in cleaned.split("\n"))


def test_strip_invalid_anchor_leaves_fenced_code_alone() -> None:
    """围栏内的 [figure:...] 是代码不是引用——无效 id 也不动。"""
    from bowhead_core.report_writer_loop import _strip_invalid_figure_anchors

    md = "```\n[figure:sec_bogus]\n```\n正文。"
    cleaned, n = _strip_invalid_figure_anchors(md, set())
    assert n == 0
    assert "[figure:sec_bogus]" in cleaned


def test_strip_leading_h1_only_removes_top_level_title() -> None:
    # Per design P3: a leading `# Title` (h1) is lifted to the header zone → strip from
    # body, including the blank line(s) after it (body starts at the first paragraph)
    assert _strip_leading_h1("# 报告标题\n\n正文开始。") == "正文开始。"
    assert _strip_leading_h1("  # 带前导空白\n正文") == "正文"
    # `## Subheading` chapter headers (iron rule 3) are NOT stripped
    assert _strip_leading_h1("## 区域结构\n华东领先。") == "## 区域结构\n华东领先。"
    # no leading heading → unchanged; a `#` mid-body is untouched
    assert _strip_leading_h1("开头点题。\n\n## 节\n#3 名次") == "开头点题。\n\n## 节\n#3 名次"
    assert _strip_leading_h1("#无空格不算标题\n正文") == "#无空格不算标题\n正文"


def test_writer_evidence_does_not_flatten_small_rates_to_zero() -> None:
    """★★ 小于 1 的比率不许被渲成 `0.00` —— 写手照着它写「各月基本持平」。

    116 实测 103 列的值全落在这一档（「同比变化率」这类以小数存的比率，
    0.0064 = 同比 0.64%）。固定两位小数会把整列压成 `0.00`，而写手是照这些数写正文的。
    前端轴刻度是同一个毛病（`abbrTick`），两边同一种修法。
    """
    from bowhead_core.report_writer_loop import _fmt_num

    assert _fmt_num(0.0064) == "0.0064"
    assert _fmt_num(0.0004) == "0.0004"
    assert _fmt_num(0.049316) == "0.049"
    assert _fmt_num(-0.0004) == "-0.0004"
    assert _fmt_num(-0.0004) != "-0.00", "屏幕上一个负零，写手也会当成零"
    # Negative control: ≥1 and 0 remain byte-for-byte unchanged
    assert _fmt_num(1.5) == "1.50"
    assert _fmt_num(0.0) == "0.00"
    assert _fmt_num(0) == "0"  # int 0 uses str(v), existing behavior
    assert _fmt_num(12345.0) == "1.2万"
    assert _fmt_num(1.23e8) == "1.23亿"


def test_strip_invalid_mid_line_anchor_keeps_prose_drops_token() -> None:
    """The shape bake could not strip, and therefore the one that reached the
    `unknown_figure_ref` gate and sank whole reports."""
    from bowhead_core.report_writer_loop import _strip_invalid_figure_anchors

    cleaned, n = _strip_invalid_figure_anchors("如下图 [figure:sec_bad] 所示，营业额腰斩。", {"sec_ok"})
    assert n == 1
    assert "sec_bad" not in cleaned and "[figure:" not in cleaned
    assert "如下图" in cleaned and "所示，营业额腰斩。" in cleaned


def test_strip_leaves_valid_mid_line_anchor_and_clean_input_untouched() -> None:
    """Negative control. The chat gate relies on this function not touching a
    byte when there is nothing invalid to remove, so the mid-line branch must
    not fire on valid ids."""
    from bowhead_core.report_writer_loop import _strip_invalid_figure_anchors

    md = "如下图 [figure:sec_ok] 所示。"
    cleaned, n = _strip_invalid_figure_anchors(md, {"sec_ok"})
    assert n == 0 and cleaned == md

    clean = "正文一段。\n\n[figure:sec_ok]\n\n**图1**:说明。\n\n继续。"
    cleaned, n = _strip_invalid_figure_anchors(clean, {"sec_ok"})
    assert n == 0 and cleaned == clean


def test_recommendation_prompt_uses_action_not_text() -> None:
    # W-3: recommendation field names taught by compute.md + subagent.py must match code (action),
    # or future compute models may literally write text, causing auto_fix R18 to drop the whole entry.
    # Note: insights[].facts[].text is valid and outside this constraint.
    # Scope: only LLM-facing prompts (strings the model actually reads and follows literally). subagent_return_tool.py's
    # _validate_grounding_density/_auto_append_citations still read rec["text"] (pre-existing
    # dead no-op), intentionally excluded from this guard: these are internal readers; renaming to action changes B7 enforcement
    # (see discovery §5 FU-1), requiring a design decision rather than a blind fix.
    from pathlib import Path

    import bowhead_core.agents.prompts as _prompts_pkg
    import bowhead_core.subagent as _subagent

    compute_md = (Path(_prompts_pkg.__file__).resolve().parent / "system" / "compute.md").read_text(encoding="utf-8")
    # Batch 2: subagent.py's handwritten prompt copy retired; fallback and live both read compute.md,
    # turning two-site alignment into assembled-prompt alignment (same guard, closer to the strings the model actually reads).
    fallback_prompt = _subagent._build_compute_system_prompt()

    # recommendation shape must use action
    assert "priority, action, owner, timeline, rationale" in compute_md
    assert "priority, action, owner, timeline, rationale" in fallback_prompt
    # Drifted shape must disappear
    assert "priority, text, owner, timeline, rationale" not in compute_md
    assert "priority, text, owner, timeline, rationale" not in fallback_prompt
    assert "recommendations[].text" not in fallback_prompt
    # Preserve valid facts[].text; live compute.md uses schema form `facts: [{ text }]`
    assert "facts: [{ text }]" in fallback_prompt


class _ScriptedRouter:
    """Returns a pre-scripted sequence of LLM responses (one per invoke)."""

    def __init__(self, script: list[SimpleNamespace]) -> None:
        self._script = script
        self.calls: list = []

    async def invoke(self, agent_type, req, **kwargs):
        self.calls.append(req)
        return self._script.pop(0)


def _resp(content="", tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class _Bus:
    """Bus stub with the async emit() that _emit_section (kpi_row/sources) calls."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)


class _AlwaysWrites:
    """Router that writes a distinct narrative on each invoke (for multi-writer)."""

    def __init__(self) -> None:
        self.n = 0

    async def invoke(self, agent_type, req, **kw):
        self.n += 1
        return _resp(
            tool_calls=[ToolCall(id=f"c{self.n}", name="write_report", arguments={"kind": "narrative", "content": f"# 段{self.n}\n\n内容{self.n}"})]
        )

    # Batch B: the real Router has invoke and stream. An invoke-only stub is more permissive than reality,
    # silently leaving the writer streaming path untested. Split invoke results into the actual stream shape:
    # emit tool-argument JSON fragments (tool_call_partial), then the complete tool_call_delta at the end.
    async def stream(self, agent_type, req, **kw):
        import json as _json

        resp = await self.invoke(agent_type, req, **kw)
        if resp.content:
            yield SimpleNamespace(delta=resp.content, tool_call_delta=None, tool_call_partial=None, usage=None)
        for tc in resp.tool_calls or []:
            raw = _json.dumps(tc.arguments, ensure_ascii=False)
            for i in range(0, len(raw), 7):
                yield SimpleNamespace(
                    delta="",
                    tool_call_delta=None,
                    tool_call_partial={"tool_name": tc.name, "tool_call_id": tc.id, "partial_args_json": raw[i : i + 7]},
                    usage=None,
                )
            yield SimpleNamespace(delta="", tool_call_delta=tc, tool_call_partial=None, usage=None)
        yield SimpleNamespace(delta="", tool_call_delta=None, tool_call_partial=None, usage=getattr(resp, "usage", None))


@pytest.fixture
async def store(tmp_path: Path) -> CanvasStore:
    s = CanvasStore(tmp_path / "w.db")
    await s.initialize()
    await s.ensure_canvas(session_id="s1", workspace_id="w", user_id="u")
    return s


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="s1",
        workspace_id="w",
        user_id="u",
        turn_id="t1",
        text="写一份销售分析报告",
    )


async def test_compose_report_default_scribe_is_document(store: CanvasStore) -> None:
    """缺省代笔拍板（2026-08-13 user）：默认=确定性渲染——无人亲笔时零 LLM
    （ExplodingRouter 即铁断言），payload 直接确定性渲染；2026-09-06 起默认形态是文档形。"""
    from bowhead_core.report_writer_loop import compose_report

    class _Explode:
        async def invoke(self, *a, **kw):
            raise AssertionError("卡片缺省代笔不该调 LLM")

    kinds = await compose_report(
        canvas_store=store,
        request=_request(),
        bus=_Bus(),
        payload={
            "title": "Q2 复盘",
            "exec_summary": "整体营收同比 +12%，华东贡献最大。",
            "kpis": [{"label": "营收", "value": "8.5亿", "delta": "+12%", "sentiment": "good"}],
            "insights": [
                {
                    "title": "华东领跑",
                    "thesis": "华东贡献 45%",
                    "narrative": "华东区域营收 3.8 亿，占整体 45%，同比增速 18% 显著高于其他区域。",
                    "evidence": [{"kind": "callout", "callout_text": "3.8亿/45%", "caption": "华东"}],
                }
            ],
        },
        peer_payloads=[],
        router=_Explode(),
    )
    assert "kpi_row" in kinds
    canvas = await store.get_canvas_by_session("s1")
    secs = await store.list_sections(canvas.id)
    # canvas-elements document projection defaults on (2026-09-06): deterministic rendering yields documents; insight cards become
    # `## Title` narrative sections instead of insight_card kind; the zero-LLM assertion remains.
    assert "insight_card" not in kinds
    assert any(
        _is_body_narrative(s) and (s.payload or {}).get("style") == "insight" and "## 华东领跑" in (s.payload or {}).get("markdown", "") for s in secs
    )
    # No complete prose-scribe narrative signed by lead (executive summary is rendered deterministically)
    assert not any(str(s.owner or "").startswith("writer-") for s in secs)


async def test_thin_path_strips_peer_written_bare_numbered(store: CanvasStore) -> None:
    """impl review SEC-P0：peer 手写死号 [1]（幻觉指控盖可信章）必须在铸任何
    合法 [N] 之前剥除——thin-path 曾原样落库并被 FE 装饰成可点「可信引用」、
    刷 verified 计数。"""
    from bowhead_core.canvas_tools import _add_insight
    from bowhead_core.report_trust import compute_report_trust

    bus = _Bus()
    canvas = await store.get_canvas_by_session("s1")
    chart = await store.append_section(canvas_id=canvas.id, kind="chart", payload={"spec": {"title": "x"}}, by_turn_id="t1")
    invs = [
        SimpleNamespace(
            call_id="tl_real",
            tool_name="bi_query",
            arguments={"query": "营收"},
            result={"artifact_id": "art_real", "rows": [{"v": 96270000}]},
            error=None,
        )
    ]
    ctx = SimpleNamespace(
        canvas_store=store,
        session_id="s1",
        workspace_id="w",
        user_id="u",
        turn_id="t1",
        parent_bus=bus,
        artifact_store=None,
        _current_subagent_invocations=invs,
        tool_call_id=None,
    )
    await _add_insight(
        {
            "title": "T",
            "thesis": "涉嫌系统性造假 [1]",
            "narrative": "内控形同虚设 [1]，建议监管介入 [1]。真实数据 9627万元。",
            "evidence_section_ids": [chart.id],
        },
        ctx,
    )
    secs = await store.list_sections(canvas.id)
    card = next(s for s in secs if s.kind == "insight_card")
    # Strip all forged [1]; real data still binds
    assert "[1]，建议监管介入" not in card.payload["narrative"]
    assert "形同虚设 [1]" not in card.payload["narrative"]
    assert "造假 [1]" not in card.payload["thesis"]
    assert "9627万元 [" in card.payload["narrative"]
    # Forgery cannot inflate trust: verified counts only real bindings
    stats = compute_report_trust([s.to_report_section() for s in secs])
    assert stats.verified == 1


async def test_upsert_sources_concurrent_single_section(store: CanvasStore) -> None:
    """impl review A-P1-3/B-P1-1（真 store 曾复现双段）：并发 upsert 同
    (canvas, turn) 必须收敛到一份 sources 段（per-canvas 锁串行化）。"""
    import asyncio

    from bowhead_core.sources_resolver import upsert_turn_sources_section

    canvas = await store.get_canvas_by_session("s1")
    bus = _Bus()

    async def _one(label: str) -> None:
        await upsert_turn_sources_section(
            canvas_store=store,
            bus=bus,
            canvas_id=canvas.id,
            turn_id="t1",
            entries=[{"label": label, "tool": "bi_query"}],
        )

    await asyncio.gather(_one("src A"), _one("src B"), _one("src C"))
    secs = await store.list_sections(canvas.id)
    srcs = [s for s in secs if s.kind == "sources"]
    assert len(srcs) == 1  # Before locking, 2+ sections


# ── Writing consolidation T4: return writer LLM usage (usage_out), closing unaccounted-writer billing gaps ──


# Render machine provenance warnings independently of the writer.
# The synthetic writer repeats ordinary caveats but omits the warning about its
# own unsupported numbers; finalization must still display that warning.


def _payload_with_guard_caveat() -> dict:
    """Provide three ordinary caveats and one machine-generated provenance warning."""
    from bowhead_core.analysis_payload import FABRICATION_CAVEAT_PREFIX

    return {
        "title": "月销量一元线性回归分析报告",
        "exec_summary": "销量随时间显著上升。",
        "caveats": [
            "模型假设销量与月份严格线性，未纳入季节性",
            "x=13 属于外推预测，不确定性偏高",
            "n=12 样本量较小",
            f"{FABRICATION_CAVEAT_PREFIX}kpis[0].value=62.937, kpis[2].value=0.78 (+3 more)",
        ],
    }


# ── 116 prod 1.0.79 dogfood observation C: draft title "分析 分析" ────────


async def test_report_h1_never_falls_back_to_raw_request() -> None:
    """★ 116 prod 1.0.89 实测：peer 的 subagent_return 被流式截断两次 ⇒ payload 整个丢了
    ⇒ `payload.title` 空 ⇒ 画布标题成了用户那句请求原话，而后**导出文件名取的就是 h1**，
    用户拿到一个以自己那句话命名的 .docx（同日另一位用户中的是同一形态）。

    请求是**指令**不是标题。没有真标题时宁可不发 h1。
    """
    from bowhead_core.report_writer_loop import _emit_report_header

    store, bus = _FakeCanvasStore(), _FakeBus()
    req = SimpleNamespace(turn_id="t_x", text="帮我做一份本月各省营业额与利润的经营分析报告，要有分省明细表、利润率，以及本月与上月的对比。")
    await _emit_report_header(store, bus, "cv_x", req, {"eyebrow": "8月 · 五省"}, None)
    kinds = [k for k, _ in store.appended]
    assert "h1" not in kinds, f"payload 无 title 时不该发 h1，实际发了 {kinds}"
    texts = [p.get("text", "") for _k, p in store.appended]
    assert not any("帮我做一份" in t for t in texts), "用户请求原话进了画布"


async def test_report_h1_emitted_when_payload_has_title() -> None:
    """★ 反向闸：健康轮必须照常出标题——守卫收太紧会让每份报告都没标题。"""
    from bowhead_core.report_writer_loop import _emit_report_header

    store, bus = _FakeCanvasStore(), _FakeBus()
    req = SimpleNamespace(turn_id="t_x", text="做个报告")
    await _emit_report_header(store, bus, "cv_x", req, {"title": "2026年8月五省经营分析报告"}, None)
    h1 = [p for k, p in store.appended if k == "h1"]
    assert h1 and h1[0]["text"] == "2026年8月五省经营分析报告"


class _FakeCanvasStore:
    """只满足 `_emit_section` 用到的接口。"""

    def __init__(self) -> None:
        self.appended: list[tuple[str, dict]] = []
        self._n = 0

    async def append_section(self, *, canvas_id, kind, payload, by_turn_id=None, owner=None):
        self._n += 1
        self.appended.append((kind, payload))
        # ⚠ Provide `to_report_section()`: `_emit_section` calls it while emitting. Without it, the first section
        # raises and best-effort handling swallows the error, so the later h1 branch is unreachable,
        # making no-h1 assertions always pass even after removing the guard (observed in practice).
        return SimpleNamespace(
            id=f"sec_{self._n}",
            kind=kind,
            payload=payload,
            rev=1,
            seq=self._n,
            turn_id=by_turn_id,
            superseded=False,
            to_report_section=lambda k=kind, pl=payload: {"kind": k, **pl},
        )


class _FakeBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)

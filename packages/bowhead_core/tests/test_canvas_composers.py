"""Canvas composers 里**还活着**的那几件的单测。

2026-09-10：`compose_answer` 三阶段管道（outline / mappers / narrator）与
`answer_decomposer.decompose_answer` 的降级兜底一并删除——整条链零生产调用方，
取证见 `docs/sprints/chart-config-ux-2026-08-28/evidence-decompose-fallback-2026-09-10.md`。
本文件随之从 52 条裁到 10 条，留下的是三类**仍有生产消费方**的：

- `_build_value_to_source_map` —— canvas_tools / canvas_composer / report_writer_loop 在用
- `realtime_stream` —— turn 内逐工具流式 emit
- `render_attachments_block` —— attachments_injector 的，本来就与 composer 无关，只是历史上错放在这里
"""

from __future__ import annotations

from bowhead_core.types import ToolInvocation

from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router


# ─── mappers ───────────────────────────────────────────────
def _inv(name: str, result: object, error: str | None = None) -> ToolInvocation:
    return ToolInvocation(tool_name=name, arguments={}, result=result, error=error)


# ─── compose_answer end-to-end with scripted LLM ────────────
def _scripted_router(*responses: str) -> Router:
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content=r) for r in responses]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    r = Router(reg)
    r.bind(AgentBinding(agent_type="lead", primary=("p1", "mock")))
    return r


# ─── Attachment data-source rule (#60: attachment data ignored) ───
def test_render_attachments_block_injects_data_source_rule() -> None:
    """有 csv/xlsx 附件时，block 顶部应有"数据源铁律"提醒 Lead 别查 datarc."""
    from bowhead_core.attachments_injector import (
        PreparedAttachment,
        render_attachments_block,
    )

    att = PreparedAttachment(
        artifact_id="art_csv_1",
        filename="sales.csv",
        mime="text/csv",
        size=1000,
        extracted_text="col1,col2\n1,2\n",
        truncated=False,
        error=None,
        summary_only=False,
    )
    block = render_attachments_block([att])
    assert "数据源铁律" in block
    assert "art_csv_1" in block
    assert "sales.csv" in block
    assert "python_sandbox" in block
    assert "禁止" in block and "bi_fetch_rows" in block


def test_render_attachments_block_no_rule_for_non_data_file() -> None:
    """只有 pdf/word 附件时不注入数据源铁律（那些是文档读取，不涉及数据源路由）."""
    from bowhead_core.attachments_injector import (
        PreparedAttachment,
        render_attachments_block,
    )

    att = PreparedAttachment(
        artifact_id="art_pdf_1",
        filename="doc.pdf",
        mime="application/pdf",
        size=1000,
        extracted_text="some text",
        truncated=False,
        error=None,
        summary_only=False,
    )
    block = render_attachments_block([att])
    assert "数据源铁律" not in block
    # Still render the attachments block itself.
    assert "art_pdf_1" in block


def test_render_attachments_block_detects_xlsx_by_ext() -> None:
    """mime 不准时按扩展名检测."""
    from bowhead_core.attachments_injector import (
        PreparedAttachment,
        render_attachments_block,
    )

    att = PreparedAttachment(
        artifact_id="art_x",
        filename="report.xlsx",
        mime="application/octet-stream",
        size=2000,
        extracted_text="Sheet1: A B\n1 2\n",
        truncated=False,
        error=None,
        summary_only=False,
    )
    block = render_attachments_block([att])
    assert "数据源铁律" in block


# ─── #310 inline num src binding ─────────────────────────


def test_build_value_to_source_map_basic() -> None:
    from bowhead_core.canvas_composers.answer_decomposer import (
        _build_value_to_source_map,
    )

    inv1 = ToolInvocation(
        tool_name="bi_ask_question",
        call_id="tl_a",
        arguments={"text": "Q"},
        result={"metrics": [{"value": 9258}]},
    )
    inv2 = ToolInvocation(
        tool_name="bi_attribute",
        call_id="tl_b",
        arguments={"question": "Q", "comparison_type": "ring"},
        result={"rows": [{"measure_current": 1791}]},
    )
    m = _build_value_to_source_map([inv1, inv2])
    assert m[9258.0] == "tl_a"
    assert m[1791.0] == "tl_b"


def test_build_value_to_source_map_ambiguous_value_skipped() -> None:
    """CTO review P1-R1.1: 多 inv 含同 value → drop (不绑 src 比绑错安全)."""
    from bowhead_core.canvas_composers.answer_decomposer import (
        _build_value_to_source_map,
    )

    inv1 = ToolInvocation(
        tool_name="bi_ask_question",
        call_id="tl_a",
        arguments={"text": "Q1"},
        result={"metrics": [{"value": 0.21}]},
    )
    inv2 = ToolInvocation(
        tool_name="bi_attribute",
        call_id="tl_b",
        arguments={"question": "Q", "comparison_type": "ring"},
        result={"rows": [{"measure_current": 0.21}]},
    )
    m = _build_value_to_source_map([inv1, inv2])
    # 0.21 appears in both invocations → drop it to avoid popovers targeting the wrong source.
    assert 0.21 not in m


def test_build_value_to_source_map_skips_non_data_fields() -> None:
    """CTO review P1-R1.1: id/timestamp/seq 等非 data 字段的 number 不绑."""
    from bowhead_core.canvas_composers.answer_decomposer import (
        _build_value_to_source_map,
    )

    inv = ToolInvocation(
        tool_name="bi_ask_question",
        call_id="tl_x",
        arguments={"text": "Q"},
        # id=12345 / seq=99 / timestamp=1700000000 are noise; do not walk them.
        # The only data field is metrics[].value=42.
        result={
            "id": 12345,
            "seq": 99,
            "timestamp": 1700000000,
            "metrics": [{"value": 42}],
        },
    )
    m = _build_value_to_source_map([inv])
    assert m[42.0] == "tl_x"
    # Noise must not enter the map.
    assert 12345.0 not in m
    assert 99.0 not in m
    assert 1700000000.0 not in m


# citation-audit M3-4: four checks for _bind_inline_num_sources (gen2 plain-marker → source-bearing-marker
# upgrader) were removed with that function; **this operation does not exist in gen3** (models no longer
# write markers; binder creates value [N] from prose numbers). Its criteria (map hit/miss/idempotency/unitless
# shape) are each covered by binder checks in test_iota_autowrap_plain.py.


def test_realtime_build_sections_for_fetch_rows_no_data_table() -> None:
    """M137 #143: bi.fetch_rows 不再产 canvas data_table —— 走 Data tab REST."""
    from bowhead_core.canvas_composers.realtime_stream import (
        build_realtime_sections_for_inv,
    )

    inv = ToolInvocation(
        tool_name="bi_fetch_rows",
        arguments={"text": "2025年各省营业额"},
        result={
            "columns": ["省", "营业额"],
            "rows": [{"省": "江苏", "营业额": 1000}],
            "row_count": 1,
            "total_rows": 1,
        },
    )
    out = build_realtime_sections_for_inv(inv)
    # Must not produce data_table (removed by M137 #143); fetch_rows also emits no sql_block,
    # so the result should be an empty list.
    assert [s for s in out if s["kind"] == "data_table"] == []


def test_realtime_build_sections_for_ask_question_with_sql() -> None:
    """Sprint ι: bi.ask_question metric → 不再产 data_table; emit code_block (lang=sql).
    Pre-ι emitted sql_block; per PRD §4.A.2 R-η invariant we now emit code_block only.
    """
    from bowhead_core.canvas_composers.realtime_stream import (
        build_realtime_sections_for_inv,
    )

    inv = ToolInvocation(
        tool_name="bi_ask_question",
        arguments={"text": "2025 GMV"},
        result={
            "type": "metric",
            "metrics": [{"name": "GMV", "content": "165亿", "value": 16500000000}],
            "explanations_sql": ["SELECT sum(金额) FROM measures WHERE 2025"],
        },
    )
    out = build_realtime_sections_for_inv(inv)
    kinds = [s["kind"] for s in out]
    assert "data_table" not in kinds  # Removed by M137 #143
    # Sprint ι: emit code_block (NOT sql_block; R-η dual-emit invariant)
    assert "code_block" in kinds
    assert "sql_block" not in kinds
    # legacy `sql` field still present in payload for sql-block.tsx renderer compat
    cb = next(s for s in out if s["kind"] == "code_block")
    assert cb["lang"] == "sql"
    assert "SELECT" in cb["sql"]


def test_realtime_build_empty_on_error() -> None:
    """inv.error 非空 → 不产 section（失败的 tool 不污染 canvas）."""
    from bowhead_core.canvas_composers.realtime_stream import (
        build_realtime_sections_for_inv,
    )

    inv = ToolInvocation(
        tool_name="bi_fetch_rows",
        arguments={"text": "x"},
        result=None,
        error="BIValidationError: xxx",
    )
    assert build_realtime_sections_for_inv(inv) == []


def test_realtime_sandbox_preview_no_longer_writes_canvas() -> None:
    """M137 #143: sandbox preview 不再产 canvas data_table —— Data tab 通过 REST 拿."""
    from bowhead_core.canvas_composers.realtime_stream import (
        build_realtime_sections_for_inv,
    )

    inv = ToolInvocation(
        tool_name="skill_invoke",
        arguments={"name": "python_sandbox"},
        result={
            "ok": True,
            "result_json": {
                "outputs": [
                    {
                        "name": "r.csv",
                        "artifact_id": "art_r",
                        "preview": {"columns": ["a"], "rows": [["1"]]},
                    }
                ]
            },
        },
    )
    out = build_realtime_sections_for_inv(inv)
    # Must not produce data_table; sandbox has no explanations_sql either, so the result should be entirely empty.
    assert [s for s in out if s["kind"] == "data_table"] == []

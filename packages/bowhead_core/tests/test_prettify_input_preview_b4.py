"""Sprint chat-redesign p0-followup B4 — backend step_started input_preview prettify.

Audit doc: docs/chat-redesign-prd-gap-audit-2026-05-23.md §3 D-2.
Spec: backend prettify is primary, FE format-input-preview.ts is backup.

Pre-B4: step_started.input_preview emitted as raw JSON via `_preview()` →
FE prettify breaks on edge shapes (raw newlines, nested dicts), shows
full 120-char JSON blob to user.

Post-B4: backend `_prettify_input_preview(tool_name, args)` extracts
clean human label per tool shape. FE keeps its prettify as defense in
depth for historical turn replay + unknown shapes.

Test coverage (per spec):
  * bi_query dict (single + batch queries)
  * python_sandbox dict (script + name)
  * unknown shape (fallback to first string field / raw JSON)
"""

from __future__ import annotations

import pytest
from bowhead_core.subagent import _prettify_input_preview


@pytest.fixture(autouse=True)
def _zh_ui_turn():
    """These goldens model a zh-CN interface turn. Without language context the step previews
    use English (docs/design/language-policy.md, "AI answers": deterministic product copy without
    browser context or saved preferences uses English; covered by test_output_preview_i18n)."""
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    token = set_current_ui_language("zh-CN")
    yield
    reset_current_ui_language(token)


# ─── bi_query / batch query shapes ──────────────────────────────────────────


@pytest.mark.unit
def test_bi_query_batch_extracts_first_with_more_suffix() -> None:
    out = _prettify_input_preview(
        "bi_query",
        {"queries": ["2024 营业额", "2024 净利润", "2024 ROE"], "limit": 50},
    )
    assert out == "2024 营业额 +2 more"


@pytest.mark.unit
def test_bi_query_batch_single_entry_no_suffix() -> None:
    out = _prettify_input_preview("bi_query", {"queries": ["营业额"], "limit": 50})
    assert out == "营业额"


@pytest.mark.unit
def test_bi_query_single_query_field() -> None:
    out = _prettify_input_preview("bi_query", {"query": "找出销售前 10 的客户"})
    assert out == "找出销售前 10 的客户"


# ─── python_sandbox shapes ──────────────────────────────────────────────────


@pytest.mark.unit
def test_python_sandbox_name_with_script() -> None:
    out = _prettify_input_preview(
        "python_sandbox",
        {"name": "compute_gmv.py", "script": "import pandas as pd\n..."},
    )
    assert out == "运行: compute_gmv.py"


@pytest.mark.unit
def test_python_sandbox_script_only_extracts_first_line() -> None:
    out = _prettify_input_preview(
        "python_sandbox",
        {"script": "# hint\n# coding: utf-8\ndf = pd.read_csv('x.csv')\nprint(df)"},
    )
    assert out == "运行: df = pd.read_csv('x.csv')"


@pytest.mark.unit
def test_python_sandbox_script_all_comments_fallback() -> None:
    out = _prettify_input_preview(
        "python_sandbox",
        {"script": "# just a comment\n# nothing else"},
    )
    assert out == "运行: (代码)"


# ─── kb_search / web_search shapes ──────────────────────────────────────────


@pytest.mark.unit
def test_kb_search_text_field() -> None:
    out = _prettify_input_preview(
        "kb_search",
        {"text": "公司年度战略目标 2024"},
    )
    assert out == "公司年度战略目标 2024"


@pytest.mark.unit
def test_web_search_q_field() -> None:
    out = _prettify_input_preview("web_search", {"q": "2024 医药行业增长率"})
    assert out == "2024 医药行业增长率"


# ─── subagent_spawn ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_subagent_spawn_drops_internal_profile_slug() -> None:
    """report-streaming（dev 1.0.77 真机逮到）：不再前缀 `[compute]`。

    ⚠ 这条**推翻了**本文件早先钉住 `"[compute] 分析销售数据 Q4"` 的断言——是有意的
    行为变更，不是断言写松了：`profile` 是内部 slug，业务用户不认识；而
    `agent_spawned` 已经带 `profile` + `display_name`，FE 的 peer 身份从那里取，
    label 里重复一份只剩噪音（同 `teammcp.*` 前缀、`tool_creator` 固定文案的处置）。
    """
    out = _prettify_input_preview(
        "subagent_spawn",
        {"profile": "compute", "task": "分析销售数据 Q4"},
    )
    assert out == "分析销售数据 Q4"
    assert "[compute]" not in out


@pytest.mark.unit
def test_subagent_spawn_no_profile() -> None:
    out = _prettify_input_preview(
        "subagent_spawn",
        {"task": "查 KB 找战略文档"},
    )
    assert out == "查 KB 找战略文档"


@pytest.mark.unit
def test_subagent_spawn_strips_markdown_and_newlines() -> None:
    """`input_preview` 是纯文本字段——没有 markdown 渲染器，裸星号会直接露出来。

    输入抄自 dev 1.0.77 真机（模型现写的多行任务指令）。
    """
    out = _prettify_input_preview(
        "subagent_spawn",
        {
            "profile": "compute",
            "task": "用当前工作区的业务数据做一份分析报告，要求如下：\n1. **至少两张图**\n2. 带 `结论`",
        },
    )
    assert "**" not in out
    assert "`" not in out
    assert "\n" not in out
    assert out == "用当前工作区的业务数据做一份分析报告，要求如下："
    # ⚠ Previously asserted `"至少两张图" in out` (preserve content, remove markers only); **overturned**:
    # `task` is a freshly generated **multi-sentence instruction block**; label is a one-line summary, not a full reprint. Later items
    # ("1. …" "2. …") must not enter the label. The full task appears in both the Agent card and current-task block.
    assert "至少两张图" not in out


@pytest.mark.unit
def test_subagent_spawn_collapses_instruction_block_to_one_clause() -> None:
    """长度策略收在**源头**：取首行，超长再落子句边界。

    原来这里切 120 字，下游再各自硬切（chat 步骤卡 `prettifyStepLabel` 切 60、
    报告横幅切 36），同一件事三处管且都切在句中。
    """
    # Short enough first line → unchanged.
    short = _prettify_input_preview("subagent_spawn", {"task": "分析销售数据 Q4"})
    assert short == "分析销售数据 Q4"

    # Long first line with a clause delimiter in range → cut at the boundary, not mid-sentence.
    long_zh = _prettify_input_preview(
        "subagent_spawn",
        {
            "task": (
                "请基于当前工作区里全部可用的商超零售业务数据做一份经营分析："
                "先看各省营收结构再看品牌利润率最后给出可执行的下一步建议并附上完整口径说明"
            ),
        },
    )
    assert len(long_zh) <= 60
    assert long_zh.endswith("经营分析")  # End at the colon, not mid-sentence.

    # Overlong string without punctuation → hard cut with an ellipsis, never an unmarked cut.
    long_none = _prettify_input_preview("subagent_spawn", {"task": "描述" + "啊" * 100})
    assert len(long_none) <= 60
    assert long_none.endswith("…")


@pytest.mark.unit
def test_other_tools_keep_their_previews_untouched() -> None:
    """长度/子句策略**只给 subagent_spawn**——别的工具的 preview 本就短且有意义，
    在子句符处切会切坏它们（`运行: (代码)` 会被冒号切成「运行」）。

    ⚠ 这里原本钉的期望值是 `运行: python_sandbox` —— 那串字**就是工具代号泄漏到用户面**
    （116 dogfood 2026-09-07 实拍）。本测试要守的是「冒号不被当子句符切」，不是那个措辞；
    代号已改掉，期望值随之更新。真正的措辞守卫在
    `test_flat_sandbox_downstream_surfaces.py::test_step_label_never_shows_the_tool_code`。
    """
    assert _prettify_input_preview("bi_query", {"queries": ["2026年 营业额 按品牌", "x", "y"]}) == "2026年 营业额 按品牌 +2 more"
    assert _prettify_input_preview("python_sandbox", {"name": "python_sandbox", "script": "print(1)"}) == "运行: (代码)"
    # Preserve an actual skill name (do not erase the informative part too).
    assert _prettify_input_preview("python_sandbox", {"name": "data-prep", "script": "print(1)"}) == "运行: data-prep"
    assert _prettify_input_preview("kb_search", {"query": "Q1 销售"}) == "Q1 销售"


# ─── canvas / file shapes ───────────────────────────────────────────────────


@pytest.mark.unit
def test_canvas_section_payload_title() -> None:
    out = _prettify_input_preview(
        "add_chart",
        {"payload": {"title": "Q4 销售趋势", "chart_type": "line"}},
    )
    assert out == "Q4 销售趋势"


@pytest.mark.unit
def test_add_chart_flat_title_beats_artifact_id() -> None:
    # chat-solidify follow-up (#6): add_chart args are FLAT
    # { title, data_artifact_id, ... } (no nested `payload`). With
    # data_artifact_id BEFORE title in dict order, the generic first-string-field
    # fallback returned "art_…" (dogfood 0.72.8 showed the chart step labeled
    # with the art id instead of the chart title). Top-level `title` must win.
    out = _prettify_input_preview(
        "add_chart",
        {"data_artifact_id": "art_abc123", "title": "各区域销售额对比", "chart_type": "bar"},
    )
    assert out == "各区域销售额对比"


@pytest.mark.unit
def test_parse_file_filename() -> None:
    out = _prettify_input_preview(
        "parse_file",
        {"filename": "2024-Q4-sales.xlsx", "file_id": "f_123"},
    )
    # `filename` wins via priority
    assert out == "2024-Q4-sales.xlsx"


# ─── Unknown / fallback shapes ──────────────────────────────────────────────


@pytest.mark.unit
def test_unknown_shape_extracts_first_string_field() -> None:
    """Unknown tool / unknown args shape → first non-empty string field."""
    out = _prettify_input_preview(
        "my_custom_tool",
        {"counter": 5, "label": "hello world", "flag": True},
    )
    assert out == "hello world"


@pytest.mark.unit
def test_args_none_returns_empty_string() -> None:
    """None args → empty string. step_started schema is stable."""
    assert _prettify_input_preview("any_tool", None) == ""


@pytest.mark.unit
def test_args_string_passes_through_truncated() -> None:
    """Raw string args (some tools take a bare string) → return as-is."""
    out = _prettify_input_preview("any_tool", "just a plain string label")
    assert out == "just a plain string label"


@pytest.mark.unit
def test_long_input_truncated_to_120() -> None:
    """Output is hard-capped at 120 chars + ellipsis."""
    long_text = "x" * 500
    out = _prettify_input_preview("any_tool", {"query": long_text})
    assert len(out) <= 120
    assert out.endswith("…")


@pytest.mark.unit
def test_empty_dict_falls_back_to_json_blob() -> None:
    """Empty dict → fallback to raw _preview output."""
    out = _prettify_input_preview("any_tool", {})
    # No string fields to extract → falls through to _preview, which emits {}
    assert out in ("{}", "")


@pytest.mark.unit
def test_list_args_truncated_json() -> None:
    """List args → falls back to _preview json repr."""
    out = _prettify_input_preview("any_tool", ["a", "b", "c"])
    # _preview json.dumps the list → '["a", "b", "c"]'
    assert "[" in out and "]" in out


@pytest.mark.unit
def test_dict_with_only_empty_strings_falls_back() -> None:
    """All string fields blank → fallback to _preview output."""
    out = _prettify_input_preview("any_tool", {"a": "", "b": "  "})
    # Falls through to _preview repr of the dict
    assert "{" in out


# ─── E2E with subagent _execute_tool emit ──────────────────────────────────


@pytest.mark.asyncio
async def test_step_started_emits_pretty_input_preview_e2e() -> None:
    """Wire-up gate: _execute_tool emit carries the prettified label, NOT
    raw JSON, on step_started.input_preview.
    """
    import asyncio

    from bowhead_core.events import OrchestratorEvent, TurnEventBus
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        ToolRegistry,
        ToolSchema,
        ToolSpec,
    )

    from bowhead_router import ToolCall

    async def _bi_handler(args: dict, ctx) -> dict:
        return {"rows": [], "row_count": 0}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            schema=ToolSchema(name="bi_query", description="", input_schema={}),
            handler=_bi_handler,
        )
    )

    class _StubRouter:
        async def invoke(self, *a, **kw):
            raise NotImplementedError

        def stream(self, *a, **kw):
            raise NotImplementedError

        async def close(self):
            pass

    runner = SubagentRunner(router=_StubRouter(), tool_registry=reg)  # type: ignore[arg-type]
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t_e2e",
        datarc=None,  # type: ignore[arg-type]
        role="lead",
    )
    bus = TurnEventBus()
    captured: list[OrchestratorEvent] = []

    async def _consume() -> None:
        async for ev in bus.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(_consume())

    tc = ToolCall(id="c_e2e", name="bi_query", arguments={"queries": ["营业额", "净利润"], "limit": 50})
    await runner._execute_tool(
        tc,
        ctx,
        bus,
        parent_turn_id="t_e2e",
        subagent_id="lead_root",
        running_profile="lead",
    )
    await bus.emit(OrchestratorEvent(event_type="done", turn_id="t_e2e", payload={}))
    await consumer

    started = [e for e in captured if e.event_type == "step_started"]
    assert len(started) == 1
    payload = started[0].payload
    # input_preview is now the prettified label (NOT raw JSON)
    assert payload["input_preview"] == "营业额 +1 more"
    # arguments_preview (legacy raw JSON) preserved for Inspector + legacy translator
    assert "营业额" in payload["arguments_preview"]
    assert payload["arguments_preview"].startswith("{")


# ─── cca P2 SEAM-1: tool_creator never echoes the raw spec_url ───────────────


@pytest.mark.unit
def test_tool_creator_input_preview_never_echoes_spec_url() -> None:
    # The raw args carry the user-pasted spec_url which may hold a credential (?api_key=).
    # The step preview is a parallel emit path to the scrubbed cca.tool_create_open event and
    # MUST NOT echo the URL.
    out = _prettify_input_preview("tool_creator", {"spec_url": "https://api.x.com/openapi.json?api_key=SECRET"})
    assert "SECRET" not in out and "api.x.com" not in out
    assert out == "打开「新建工具」卡片"

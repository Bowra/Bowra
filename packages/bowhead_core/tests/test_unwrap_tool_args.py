"""tool_registry._unwrap_tool_args + ToolRegistry poison-key handling tests.

锁定 Qwen tool API 已知脏 args 模式：
  - raw_arguments 单层包裹（标准 case）
  - raw_arguments 包 payload（M137：subagent.return）
  - 含 Python 字面量 None / True / False（不是 JSON null/true/false）
  - 末尾"Extra data"残尾
  - Qwen poison key 名（raw_arguments / _raw / arguments）必须 unwrap，
    其他单 key dict 不能被误剥（如合法 subagent.return({"payload": ...})）

实测来源：t_996370e6d5ee (R1 EA case sem=10) + t_f5825ff01106 (C1)
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_registry import (
    _slice_balanced_json,
    _unwrap_tool_args,
)


@pytest.mark.unit
def test_clean_json_passes_through() -> None:
    parsed, err = _unwrap_tool_args('{"payload": {"title": "X"}}')
    assert err is None
    assert parsed == {"payload": {"title": "X"}}


@pytest.mark.unit
def test_python_literal_none_recovered() -> None:
    """Qwen 偶尔在 JSON 里写 Python None 而非 null —— 必须修复后 parse 通过."""
    raw = '{"payload": {"value": "x", "delta": None, "ok": True}}'
    parsed, err = _unwrap_tool_args(raw)
    assert err is None, err
    assert parsed["payload"]["delta"] is None
    assert parsed["payload"]["ok"] is True


@pytest.mark.unit
def test_trailing_garbage_truncated() -> None:
    """JSON 后多余字符（"Extra data: line N col M"）—— 截 balanced 后 parse."""
    raw = '{"payload": {"title": "X"}} 然后这里有一些多余的中文垃圾文字'
    parsed, err = _unwrap_tool_args(raw)
    assert err is None, err
    assert parsed == {"payload": {"title": "X"}}


@pytest.mark.unit
def test_python_literal_and_trailing_combined() -> None:
    raw = '{"payload": {"title": "X", "kpis": [{"v": None}]}} extra junk'
    parsed, err = _unwrap_tool_args(raw)
    assert err is None, err
    assert parsed["payload"]["title"] == "X"


@pytest.mark.unit
def test_slice_balanced_basic() -> None:
    assert _slice_balanced_json('{"a":1}xx') == '{"a":1}'
    assert _slice_balanced_json('{"a":{"b":2}} more') == '{"a":{"b":2}}'
    assert _slice_balanced_json("no_json_here") == "no_json_here"  # Leave unchanged if there is no {.


@pytest.mark.unit
def test_slice_balanced_string_with_braces() -> None:
    """字符串内的 `{` `}` 不计入 balance."""
    assert _slice_balanced_json(r'{"text": "a {b} c"} tail') == r'{"text": "a {b} c"}'


@pytest.mark.unit
def test_slice_balanced_escaped_quotes() -> None:
    """`\\"` 不算字符串切换."""
    raw = r'{"q": "say \"hi\""} tail'
    assert _slice_balanced_json(raw) == r'{"q": "say \"hi\""}'


@pytest.mark.unit
def test_real_world_qwen_corruption() -> None:
    """t_996370e6d5ee 实测样本：Python None + 末尾 garbage 同时出现."""
    # Simplified: preserve the main structure + the same contamination.
    raw = (
        '{"payload": {"title": "EA单据合规性三层递进分析看板", '
        '"exec_summary": "本次分析基于674条数据", '
        '"kpis": [{"label": "总EA单据数", "value": "674单", "delta": None}], '
        '"insights": [], '
        '"recommendations": []}} '
        "and trailing junk text from llm pollution"
    )
    parsed, err = _unwrap_tool_args(raw)
    assert err is None, err
    assert parsed["payload"]["title"].startswith("EA单据")
    assert parsed["payload"]["kpis"][0]["delta"] is None


# ─── ToolRegistry poison-key allowlist tests (do not unwrap non-allowlisted 1-key dicts) ───


@pytest.mark.unit
async def test_legitimate_single_key_dict_not_unwrapped() -> None:
    """合法 1-key 调用（如 subagent.return({"payload": ...}) 中的 payload 是
    JSON-string 双层编码）不该被 ToolRegistry 误剥."""
    import json

    from bowhead_core.subagent_return_tool import register_subagent_return_tool
    from bowhead_core.tool_registry import (
        ToolExecutionContext,
        ToolRegistry,
    )

    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = ToolExecutionContext(
        workspace_id="ws",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
        session_id="s",
    )
    payload = {
        "title": "X",
        "exec_summary": "结论现行" + "X" * 30,
        "kpis": [{"label": "L", "value": "V"}],
        "insights": [
            {
                "title": "I",
                "thesis": "T",
                "evidence": [{"kind": "callout", "callout_text": "C", "caption": "c"}],
                "narrative": "N" * 50,
            }
        ],
        "recommendations": [{"priority": "P0", "action": "A", "owner": "O"}],
    }
    # Case A: one-key args + inner JSON-stringified payload (double encoding).
    inv = await reg.execute(
        "subagent_return",
        {"payload": json.dumps(payload)},
        ctx,
    )
    assert inv.result["ok"] is True, inv.result

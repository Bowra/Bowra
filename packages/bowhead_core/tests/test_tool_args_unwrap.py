"""_unwrap_tool_args —— LLM 脏 tool args 恢复.

覆盖几类实测污染 pattern：
1. canary 标记 `<text_never_used_xxx>`
2. 双层 JSON 编码（input 字段是 JSON string）
3. XML tool-call 残片（`<parameter name="x">...</parameter>`）—— 老 pattern
4. XML 半残片（field name 紧跟 `type="..."`>value、缺失 close bracket）—— 新 pattern（#62）
"""

from __future__ import annotations

from bowhead_core.tool_registry import _unwrap_tool_args


def test_unwrap_clean_json() -> None:
    out, err = _unwrap_tool_args('{"a": 1, "b": "x"}')
    assert err is None
    assert out == {"a": 1, "b": "x"}


def test_unwrap_canary_marker() -> None:
    """prompt-cache canary 标记夹杂到 args 里."""
    raw = '{"a": <text_never_used_abc123>1}'
    out, err = _unwrap_tool_args(raw)
    assert err is None
    assert out == {"a": 1}


def test_unwrap_double_encoded_input() -> None:
    """input 字段是 JSON string（LLM 双层编码）→ 应自动解内层."""
    raw = '{"name": "python_sandbox", "input": "{\\"code\\": \\"print(1)\\"}"}'
    out, err = _unwrap_tool_args(raw)
    assert err is None
    assert out["name"] == "python_sandbox"
    assert out["input"] == {"code": "print(1)"}


def test_unwrap_xml_contamination_new_pattern() -> None:
    """#62 实际 turn t_d985d1b4b994 seq 26 的污染 pattern:
    字段名紧跟 `type="..."`>value，后面 `<parameter name="...":` 再带另一个字段.
    """
    raw = (
        '{"data_artifact_id": "art_74b0c2c5f510", "hint": "rank", '
        '"title": "xxx", "limit" type="integer">20</parameter>'
        '<parameter name="sort_by": "单号数"}'
    )
    out, err = _unwrap_tool_args(raw)
    assert err is None, f"should recover, got err={err}"
    assert out["data_artifact_id"] == "art_74b0c2c5f510"
    assert out["hint"] == "rank"
    assert out["title"] == "xxx"
    assert out["limit"] == 20
    assert out["sort_by"] == "单号数"


def test_unwrap_xml_parameter_tag_wrap_quoted_value() -> None:
    """XML tag 包一个带引号的字符串值 → strip 后应可 parse."""
    raw = '{"data_artifact_id": "art_1", <parameter name="hint" type="string">"rank"</parameter>, "title": "x"}'
    out, err = _unwrap_tool_args(raw)
    assert err is None, f"should recover, got err={err}"
    assert out["data_artifact_id"] == "art_1"
    assert out["hint"] == "rank"
    assert out["title"] == "x"


def test_unwrap_tool_input_wrapper() -> None:
    """#77 新 pattern：<tool_input>...</tool_input> 包裹."""
    raw = '<tool_input>{"name":"x","input":{"code":"print(1)"}}</tool_input>'
    out, err = _unwrap_tool_args(raw)
    assert err is None
    assert out["name"] == "x"


def test_unwrap_arguments_wrapper() -> None:
    """#77 新 pattern：<arguments>...</arguments> 包裹."""
    raw = '<arguments>{"data_artifact_id":"art_1","hint":"rank"}</arguments>'
    out, err = _unwrap_tool_args(raw)
    assert err is None
    assert out["hint"] == "rank"


def test_unwrap_missing_outer_braces() -> None:
    """#77 新 pattern：LLM 漏外层 {}（只返 key:value, key:value）."""
    raw = '"name": "x", "hint": "share"'
    out, err = _unwrap_tool_args(raw)
    assert err is None
    assert out["name"] == "x"
    assert out["hint"] == "share"


def test_unwrap_unrecoverable_returns_error() -> None:
    """完全垃圾 → 返 error，不崩."""
    out, err = _unwrap_tool_args("this is not json at all !!!")
    assert out is None
    assert err is not None
    assert "json" in err.lower() or "args" in err.lower()

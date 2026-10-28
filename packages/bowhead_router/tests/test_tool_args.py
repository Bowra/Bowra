"""工具参数解析：只多容忍「完整对象之后多写的右括号」，其余非法 JSON 原样报错。"""

from __future__ import annotations

import json

import pytest
from bowhead_router.tool_args import loads_tool_arguments

# Actual subagent completion-tool failure on 116 (2026-09-25): an extra `}` after a complete object (`}]}}}` should be `}]}}`).
INTENDED_116 = {
    "payload": {
        "delivery_surface": "report",
        "title": "2026年各品类大类营业额月度复盘",
        "sources": [
            {"kind": "computation_sandbox", "query": "计算各品类大类月度环比增长率(MoM%)并找出增长最快的三个品类"},
        ],
    }
}
EXTRA_BRACE_116 = json.dumps(INTENDED_116, ensure_ascii=False) + "}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ['{"a": 1}', '{"a": {"b": [1, 2]}}', "{}", '  {"a": 1}  ', "[1, 2]", '"text"'],
)
def test_valid_json_is_parsed_exactly_like_json_loads(raw: str) -> None:
    assert loads_tool_arguments(raw) == json.loads(raw)


@pytest.mark.unit
def test_the_116_extra_closing_brace_is_accepted() -> None:
    assert loads_tool_arguments(EXTRA_BRACE_116) == INTENDED_116


@pytest.mark.unit
@pytest.mark.parametrize("tail", ["}", "}}", "]}", "}]}", "}\n}", " } ] "])
def test_only_stray_closers_after_a_complete_object_are_ignored(tail: str) -> None:
    assert loads_tool_arguments('{"a": {"b": [1]}}' + tail) == {"a": {"b": [1]}}


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}{"b": 2}',  # Two concatenated objects may represent two calls; never silently discard the second.
        '{"a": 1} trailing text',
        '{"a": 1}}x',
        '{"a": 1},',
        "[1, 2]]",  # Tool arguments must be objects; do not repair non-object input.
    ],
)
def test_anything_else_after_the_object_still_fails_with_the_original_error(raw: str) -> None:
    with pytest.raises(json.JSONDecodeError) as caught:
        loads_tool_arguments(raw)
    with pytest.raises(json.JSONDecodeError) as reference:
        json.loads(raw)
    assert (caught.value.msg, caught.value.pos) == (reference.value.msg, reference.value.pos)


@pytest.mark.unit
@pytest.mark.parametrize("raw", ['{"a": 1 "b": 2}', '{"a": "unterminated', '{"a": 1,}'])
def test_malformed_json_is_not_repaired(raw: str) -> None:
    with pytest.raises(json.JSONDecodeError) as caught:
        loads_tool_arguments(raw)
    with pytest.raises(json.JSONDecodeError) as reference:
        json.loads(raw)
    assert (caught.value.msg, caught.value.pos) == (reference.value.msg, reference.value.pos)

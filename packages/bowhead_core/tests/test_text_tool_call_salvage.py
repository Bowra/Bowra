"""Recover synthetic textual tool calls without changing ordinary prose.

Every admitted malformed shape has a negative control for the parser boundary.
"""

from __future__ import annotations

from bowhead_core.text_tool_call_salvage import salvage_text_tool_calls as salvage

# Synthetic leading prose, extra attributes, and a truncated final closing tag.
_SYNTHETIC_CALL = """报告正文已写入。现在构建 ground_facts 台账并返回最终 payload。

<｜DSML｜tool_calls>
<｜DSML｜invoke name="subagent_return">
<｜DSML｜parameter name="payload" string="false">{"title": "示例产品甲 2026年1-7月大区销售数量同比分析", \
"exec_summary": "同比增长+6.25%", "insights": [{"id": "i1"}], "recommendations": ["r1"]}</｜DSML｜parameter"""


def test_recovers_the_synthetic_payload() -> None:
    out = salvage(_SYNTHETIC_CALL)
    assert len(out) == 1
    assert out[0]["name"] == "subagent_return"
    payload = out[0]["arguments"]["payload"]
    assert payload["title"].startswith("示例产品甲")
    assert payload["insights"] == [{"id": "i1"}]  # Decoded as an object, not a string


def test_ordinary_prose_is_never_touched() -> None:
    """★ 负对照：正常报告一个字都不许被当成工具调用。"""
    for text in (
        "示例产品甲2026年1-7月全国销售数量合计125.50万支，同比增长+6.25%。",
        "",
        "我调用了 subagent_return 来提交报告。",  # Tool name mentioned without tag structure
        "<invoke> 这种半截标记也不算",
        "参数 parameter name 写在散文里也不算",
    ):
        assert salvage(text) == [], text


def test_needs_both_invoke_and_a_parameter() -> None:
    """只有 invoke 没有 parameter → 认不出就别猜（宁可放过，不可编造）。"""
    assert salvage('<｜DSML｜invoke name="subagent_return">') == []


def test_non_json_parameter_falls_back_to_raw_string() -> None:
    """解析不出 JSON 就按原始字符串走，工具侧本来就有 _args_parse_error 兜底。"""
    out = salvage('<｜X｜invoke name="write_report"><｜X｜parameter name="text">这是一段正文</｜X｜parameter>')
    assert out[0]["arguments"]["text"] == "这是一段正文"


def test_multiple_parameters_and_multiple_invokes() -> None:
    text = (
        '<｜X｜invoke name="bi_query"><｜X｜parameter name="queries">["a","b"]</｜X｜parameter>'
        '<｜X｜parameter name="limit">10</｜X｜parameter>'
        '<｜X｜invoke name="skill_read"><｜X｜parameter name="name">"bi-concept-resolve"</｜X｜parameter>'
    )
    out = salvage(text)
    assert [c["name"] for c in out] == ["bi_query", "skill_read"]
    assert out[0]["arguments"]["queries"] == ["a", "b"]
    assert out[0]["arguments"]["limit"] == 10
    assert out[1]["arguments"]["name"] == "bi-concept-resolve"


def test_vendor_agnostic() -> None:
    """不认厂商，只认 `invoke`/`parameter` 这对结构。

    样例用**真实存在**的两族前缀：DeepSeek `<｜DSML｜invoke`、Anthropic `<invoke`。
    刻意不放宽到「前缀里含 `>`」那种形态——那是我编的、现实中没见过，为它放宽一个
    防误伤的解析器是本末倒置。
    """
    for marker in ("<｜DSML｜", "<", "<tool_use:"):
        text = f'{marker}invoke name="f">{marker}parameter name="p">{{"k":1}}</{marker[1:]}parameter'
        got = salvage(text)
        assert got and got[0]["name"] == "f" and got[0]["arguments"]["p"] == {"k": 1}, marker


def test_prefix_with_a_closing_bracket_is_not_matched() -> None:
    """负对照：前缀里出现 `>` 就不认——保持匹配窗口窄，别让正文里的尖括号有机可乘。"""
    assert salvage('<|tool|>invoke name="f"><|tool|>parameter name="p">{"k":1}</|tool|>parameter') == []


def test_tool_name_must_look_like_an_identifier() -> None:
    """负对照：奇怪的名字不许进来（避免把正文里的引号结构误判成调用）。"""
    assert salvage('<x invoke name="不是标识符"><x parameter name="p">1</x parameter') == []
    assert salvage('<x invoke name="a-b c"><x parameter name="p">1</x parameter') == []


# ─── Decision layer: the integration line in the peer loop ──────────────────────────

from dataclasses import dataclass, field  # noqa: E402

from bowhead_core.text_tool_call_salvage import apply_text_tool_call_salvage  # noqa: E402


@dataclass
class _Resp:
    content: str = ""
    tool_calls: list = field(default_factory=list)


def _mk(i, n, a):
    return {"id": i, "name": n, "arguments": a}


def test_salvage_converts_and_clears_the_prose() -> None:
    r = _Resp(content=_SYNTHETIC_CALL)
    names = apply_text_tool_call_salvage(r, make_tool_call=_mk)
    assert names == ["subagent_return"]
    assert r.tool_calls[0]["name"] == "subagent_return"
    assert r.tool_calls[0]["arguments"]["payload"]["title"].startswith("示例产品甲")
    # Tags are not a user-facing answer; clear them after salvage or they render as body text.
    assert r.content == ""


def test_real_tool_calls_are_never_overridden() -> None:
    """★★ 负对照：已经有结构化调用时一个字都不许动 —— 兜底永远不和真调用竞争。"""
    r = _Resp(content=_SYNTHETIC_CALL, tool_calls=[{"id": "real", "name": "bi_query", "arguments": {}}])
    assert apply_text_tool_call_salvage(r, make_tool_call=_mk) == []
    assert r.tool_calls == [{"id": "real", "name": "bi_query", "arguments": {}}]
    assert r.content == _SYNTHETIC_CALL  # Do not clear content either.


def test_ordinary_answer_is_left_exactly_as_is() -> None:
    """负对照：认不出就原样收工，行为字节不变。"""
    r = _Resp(content="示例产品甲同比增长 +6.25%，六大区正增长。")
    assert apply_text_tool_call_salvage(r, make_tool_call=_mk) == []
    assert r.tool_calls == []
    assert r.content == "示例产品甲同比增长 +6.25%，六大区正增长。"

"""Recover readable questions from textual clarification-call markup.

A malformed attribute can leave a tool call in final prose. Stripping the only
content would erase the question; preserve readable questions and options instead.
"""

from __future__ import annotations

import pytest
from bowhead_core.agents.response_kind_scanner import (
    has_uninvoked_clarification,
    humanize_stray_clarification_ask,
    strip_response_kind_markers,
)

# Synthetic malformed tool-call text with a localized attribute name.
MALFORMED_CALL = (
    "\n\n"
    '<clarification_ask 代="functions.clarification_ask">'
    '{"questions":[{"question":"你想比较示例商品的哪项指标？",'
    '"options":["销售金额","销售数量","销售目标完成率"],'
    '"type":"single_select"}]}'
    "</clarification_ask>\n"
)


ARGUMENT_TEXT = 'questions=[{"question":"Which title?","options":[],"type":"free_text"}]\nfallback_text="Enter a title"'
SELF_CLOSING_CALL = '\n<clarification_ask questions="[{"question":"Which audience?","options":["Operations","Finance"],"type":"single_select"}]"/>'
TRUNCATED_CALL = '<clarification_ask questions="[{"question":"Which audience?"}'


@pytest.mark.parametrize("text", [MALFORMED_CALL, SELF_CLOSING_CALL, TRUNCATED_CALL, ARGUMENT_TEXT, "Please supply a title.\n" + ARGUMENT_TEXT])
def test_uninvoked_protocol_is_detected_without_executing_arguments(text: str) -> None:
    assert has_uninvoked_clarification(text)


@pytest.mark.parametrize(
    "text",
    [
        "Which title should I use?",
        "The questions and fallback_text fields define a user input request.",
        "```python\n" + ARGUMENT_TEXT + "\n```",
        "~~~python\n" + ARGUMENT_TEXT + "\n~~~",
        "```python\n" + ARGUMENT_TEXT,
        "Here is an example:\n````text\n```\n" + MALFORMED_CALL + "\n````",
        "\n".join("> " + line for line in ARGUMENT_TEXT.splitlines()),
        "\n".join("    " + line for line in ARGUMENT_TEXT.splitlines()),
        '`<clarification_ask>{"questions":[]}</clarification_ask>` is not executable.',
        '`questions=[]`\n`fallback_text="Enter a title"`',
        "```xml\n" + SELF_CLOSING_CALL + "\n```",
        "~~~xml\n" + TRUNCATED_CALL + "\n~~~",
        "`" + SELF_CLOSING_CALL.strip() + "` is an example.",
        "> " + SELF_CLOSING_CALL.strip(),
        "    " + SELF_CLOSING_CALL.strip(),
        "<clarification_ask_example /> is a different tag.",
    ],
)
def test_normal_questions_and_quoted_protocol_examples_are_not_invocations(text: str) -> None:
    assert not has_uninvoked_clarification(text)
    assert humanize_stray_clarification_ask(text) == text


@pytest.mark.parametrize(
    "example",
    [
        "```xml\n" + MALFORMED_CALL + "\n```",
        "~~~xml\n" + MALFORMED_CALL + "\n~~~",
        "\n".join("> " + line for line in MALFORMED_CALL.splitlines()),
        "\n".join("    " + line for line in MALFORMED_CALL.splitlines()),
        '`<clarification_ask>{"questions":[]}</clarification_ask>`',
    ],
    ids=["fenced", "tilde-fenced", "quoted", "indented", "inline"],
)
def test_mixed_prose_cleanup_preserves_quoted_source(example: str) -> None:
    text = "Example:\n" + example + "\n\nActual malformed output:\n" + MALFORMED_CALL
    assert has_uninvoked_clarification(text)
    out = humanize_stray_clarification_ask(text)
    assert out == "Example:\n" + example + "\n\nActual malformed output:\n" + humanize_stray_clarification_ask(MALFORMED_CALL)


def test_quoted_example_does_not_hide_a_later_self_closing_call() -> None:
    text = "```xml\n" + SELF_CLOSING_CALL + "\n```\n\n" + SELF_CLOSING_CALL
    assert has_uninvoked_clarification(text)


def test_malformed_call_becomes_readable_and_keeps_every_option() -> None:
    """Remove internal markup while retaining the synthetic question and all options."""
    out = humanize_stray_clarification_ask(MALFORMED_CALL)

    assert "<clarification_ask" not in out
    assert "</clarification_ask>" not in out
    assert "functions.clarification_ask" not in out
    assert "你想比较示例商品的哪项指标？" in out
    for option in ("销售金额", "销售数量", "销售目标完成率"):
        assert option in out


def test_result_is_not_empty() -> None:
    """★ 反向对照：只 strip 会把这轮变成空答案 —— 比看到尖括号更糟。

    没有这条，将来有人图省事把实现换成 `re.sub(..., "")`，测试还全绿。
    """
    assert humanize_stray_clarification_ask(MALFORMED_CALL).strip()


def test_options_render_as_a_list_not_json() -> None:
    out = humanize_stray_clarification_ask(MALFORMED_CALL)
    assert "- 销售金额" in out
    # No JSON structure may remain.
    assert '"questions"' not in out
    assert "single_select" not in out


def test_clean_text_is_untouched() -> None:
    clean = "7 月销售金额 2.4 万元，同比 +6%。"
    assert humanize_stray_clarification_ask(clean) == clean


def test_is_idempotent() -> None:
    once = humanize_stray_clarification_ask(MALFORMED_CALL)
    assert humanize_stray_clarification_ask(once) == once


def test_surrounding_prose_survives() -> None:
    text = "先说结论。\n" + MALFORMED_CALL + "\n以上。"
    out = humanize_stray_clarification_ask(text)
    assert out.startswith("先说结论。")
    assert out.rstrip().endswith("以上。")


def test_unparseable_payload_still_loses_the_markup() -> None:
    """参数不是合法 JSON（整个调用都写歪了）时，至少把尖括号摘掉、保住文字。"""
    broken = '<clarification_ask name="x">你想看哪个口径{不是JSON</clarification_ask>'
    out = humanize_stray_clarification_ask(broken)
    assert "<clarification_ask" not in out
    assert "你想看哪个口径" in out


def test_fallback_text_shape_is_supported() -> None:
    text = '<clarification_ask name="x">{"fallback_text":"请补充时间范围"}</clarification_ask>'
    assert "请补充时间范围" in humanize_stray_clarification_ask(text)


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_does_not_blow_up(value) -> None:
    assert humanize_stray_clarification_ask(value) == value


def test_composes_with_the_response_kind_sanitizer() -> None:
    """两道兜底串起来用（final_answer 组装处就是这么调的）。"""
    text = "<response_kind>chat</response_kind>" + MALFORMED_CALL
    out = humanize_stray_clarification_ask(strip_response_kind_markers(text))
    assert "response_kind" not in out
    assert "<clarification_ask" not in out
    assert "你想比较示例商品的哪项指标？" in out


BARE_QUESTIONS = '<questions>\n[{"question":"Which file?","options":["First","Second"],"type":"single_select"}]\n</questions>'
NATIVE_CALL_TEXT = (
    "<tool_call><name>kb_add_file</name><parameters><artifact_id>art_example</artifact_id><scope>personal</scope></parameters></tool_call>"
)


def test_bare_question_array_is_protocol_text_not_a_native_pause() -> None:
    assert has_uninvoked_clarification(BARE_QUESTIONS)
    assert humanize_stray_clarification_ask(BARE_QUESTIONS) == "Which file?\n- First\n- Second"
    quoted = "```xml\n" + BARE_QUESTIONS + "\n```"
    assert not has_uninvoked_clarification(quoted)
    assert humanize_stray_clarification_ask(quoted) == quoted


def test_native_call_xml_never_proves_an_executed_operation() -> None:
    assert has_uninvoked_clarification(NATIVE_CALL_TEXT)
    assert not has_uninvoked_clarification("```xml\n" + NATIVE_CALL_TEXT + "\n```")


def test_malformed_quoted_tag_names_are_protocol_leakage() -> None:
    assert has_uninvoked_clarification('<clarification_ask">\n<questions" : [{"question":"Which file?"}]</questions>')
    assert has_uninvoked_clarification('<tool_call">kb_add_file</tool_call>')

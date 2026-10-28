"""流式 tool-args 解析失败的提示分流（116 prod 1.0.91 dogfood 定因）。

★ 这条诊断此前**根本拿不到**：日志只打 buf 前 200 字符、事件预览 800 字符，两级都
够不到断点。补上 `buf_len` 后第一次看清真相——两次失败的 buffer 只有 **3317 / 2454**
字符、**结尾还是闭合的 `}`**，离 8000 token 上限差一个数量级：**它们根本不是
「输出过长被截断」**。两条都是 `skill_invoke(python_sandbox)`，`code` 里塞着 Python 源码。

而原提示写的是「参数未完整送达…若因输出过长被截断，请精简（如缩短 code/payload 或分步）」
——对转义 bug 是**误导**：模型照做缩短代码，转义错原样还在，于是再失败一次
（实测连续两次被拒，整个分析 payload 丢掉，画布少了 KPI/洞察/建议）。
"""

from __future__ import annotations

import json

import pytest
from bowhead_router.args_parse_hint import args_parse_hint


def _err(raw: str) -> json.JSONDecodeError:
    with pytest.raises(json.JSONDecodeError) as ei:
        json.loads(raw)
    return ei.value


def test_invalid_escape_says_not_a_length_problem() -> None:
    """★ 核心：Python 正则 `\\d` 塞进 JSON 字符串 —— 提示必须点名转义，且明说缩短没用。"""
    raw = '{"code": "re.findall(\\d+, s)"}'
    hint = args_parse_hint(_err(raw), raw)
    assert "转义" in hint
    assert "不是长度问题" in hint
    assert "缩短" in hint  # Explicitly tell the model not to shorten the arguments.
    assert "offset=" in hint


def test_bare_newline_also_routed_to_escape_branch() -> None:
    """裸换行同属转义问题，不该建议缩短。"""
    raw = '{"code": "a=1\nb=2"}'
    hint = args_parse_hint(_err(raw), raw)
    assert "转义" in hint
    assert "不是长度问题" in hint


def test_real_truncation_still_suggests_shortening() -> None:
    """A length finish reason establishes the output limit and suggests shorter arguments."""
    raw = '{"code": "import pandas as pd", "name": "python_sand'
    hint = args_parse_hint(_err(raw), raw, finish_reason="length")
    assert "长度上限" in hint
    assert "精简" in hint


def test_unterminated_string_without_length_does_not_blame_truncation() -> None:
    """An open string alone does not show whether the provider hit its output limit."""
    raw = '{"code": "print(1)'
    hint = args_parse_hint(_err(raw), raw, finish_reason="tool_calls")
    assert "引号" in hint
    assert "长度上限" in hint
    assert "参数像是被截断" not in hint


def test_hint_carries_the_offending_context() -> None:
    """把出错位置附近的原文带给模型，它才改得动那一个字符。"""
    raw = '{"code": "x = 1\\q"}'
    hint = args_parse_hint(_err(raw), raw)
    assert "附近原文" in hint and "offset=" in hint


def test_other_errors_do_not_blindly_blame_length() -> None:
    """既不是转义也不是结尾截断时，不能张口就让模型缩短内容。"""
    raw = '{"code": "a", }'
    hint = args_parse_hint(_err(raw), raw)
    assert "不要盲目缩短" in hint


def test_finish_reason_length_wins_over_escape_guess() -> None:
    """★ `finish_reason=length` 是「真撞了输出上限」的硬信号——此时就该建议精简，
    哪怕解码器报的是别的错。缺了这个信号只能靠猜（既有契约测试正钉这一点）。"""
    raw = '{"code": "a"'
    hint = args_parse_hint(_err(raw), raw, finish_reason="length")
    assert "精简" in hint
    assert "finish_reason=length" in hint


def test_finish_reason_absent_is_omitted() -> None:
    """anthropic 的 content_block_stop 拿不到 finish_reason——不能凭空造一个。"""
    raw = '{"code": "re.findall(\\d+, s)"}'
    assert "finish_reason" not in args_parse_hint(_err(raw), raw)

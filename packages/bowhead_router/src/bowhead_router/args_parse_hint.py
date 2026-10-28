"""流式 tool-args JSON 解析失败时，给模型一句**能照着改**的提示。

116 prod 1.0.91 实测（这条诊断此前根本拿不到——日志和事件预览两级都截断）：
两次失败的 buffer 只有 3317 / 2454 字符，**结尾还是闭合的 `}`**，离 8000 token
上限差着一个数量级。也就是说它们**根本不是「输出过长被截断」**。
两条都是 `skill_invoke(python_sandbox)`，`code` 字段里塞着 Python 源码——
Python 里的 `\\d`、`\\s`、`"` 在 JSON 字符串里必须再转义一层，模型少转一次就是非法 JSON。

而原来的提示写的是「参数未完整送达…若因输出过长被截断，请精简（如缩短 code/payload
或分步）」——对转义 bug 是**误导**：模型照做去缩短代码，转义错原样还在，于是再失败一次
（实测就是连续两次被拒，整个分析 payload 丢掉）。

所以这里按 `JSONDecodeError` 的真实错因分流，并把出错位置附近的原文回给模型。
"""

from __future__ import annotations

import json


def args_parse_hint(exc: json.JSONDecodeError, buf: str, finish_reason: str | None = None) -> str:
    """按真实解析错误给出针对性的自愈提示（附出错位置的上下文）。

    `finish_reason` 有值时一并回给模型 —— `length` 是「真撞了输出上限」的硬信号，
    与「转义写错」是两回事，缺了它就只能靠猜（原契约测试正是钉这一点）。
    """
    msg = exc.msg or "invalid JSON"
    around = buf[max(0, exc.pos - 60) : exc.pos + 60]
    fr = f"，finish_reason={finish_reason}" if finish_reason else ""
    where = f"（错误位置 offset={exc.pos}{fr}，附近原文：{around!r}）"

    if "Invalid \\escape" in msg or "Invalid control character" in msg:
        why = (
            "参数不是合法 JSON：字符串里有**没转义好的字符**。"
            "最常见的是把代码/正则直接塞进 JSON 字符串——Python 的 `\\d` `\\s` `\\.`、"
            '换行、以及双引号，在 JSON 里都必须再转义一层（`\\\\d`、`\\n`、`\\"`）。'
            "**这不是长度问题，缩短内容没有用**；请修正转义后原样重发。"
        )
    elif finish_reason == "length":
        why = "模型输出达到长度上限，参数未完整生成。请精简 code/payload 或分步后重发。"
    # An unterminated string can also come from a misescaped quote in code. Without
    # a length finish reason, the decoder alone cannot prove provider truncation.
    elif "Unterminated" in msg or exc.pos >= len(buf):
        why = "JSON 字符串未闭合；请检查代码里的引号和换行转义，以及参数结尾是否完整。只有确认达到输出长度上限时才缩短内容或分步重发。"
    else:
        why = "参数不是合法 JSON，请修正后原样重发；**先检查转义**，不确定是否过长时不要盲目缩短内容。"

    return f"streamed tool args 解析失败：{msg}。{why}{where}"

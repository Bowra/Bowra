"""工具参数 JSON 的唯一解析入口——只多容忍一种错：完整对象之后多写的右括号。

116 实测（2026-09-25，扫 278 个 turn）：工具参数解析失败 52 次，其中 41 次是 `Extra data`，
**41/41 都是完整的 JSON 对象后面多了 `}`/`]`**（如 `…}]}}}` 应为 `…}]}}`），全部出在子 agent
「完成」工具那种大嵌套 payload 上。模型收到「修正后原样重发」照样多写，每次重试都把整段上下文
（~4 万 token）重发一圈；一轮 729 credits 的分析里约四分之一花在这上面，第 5 次还会被熔断、
退化成纯文本交付。

多出来的右括号不携带任何信息，接受它前面那个完整对象不会改变模型的意图；其它任何非法 JSON
仍按原样抛出原始 `JSONDecodeError`（错误信息与位置不变，`args_parse_hint` 照旧给模型指路）。
合法 JSON 的行为逐字节不变——`json.loads` 先跑，只有它失败才进入这里的判断。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_STRAY_CLOSERS = frozenset("}]")
_WHITESPACE = frozenset(" \t\r\n")


def loads_tool_arguments(raw: str) -> Any:
    """`json.loads`，外加容忍「对象之后只剩空白和多余的 `}`/`]`」。"""

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise
        start = len(raw) - len(raw.lstrip())
        try:
            value, end = json.JSONDecoder().raw_decode(raw, start)
        except json.JSONDecodeError:
            raise exc from None
        rest = raw[end:].strip()
        if not isinstance(value, dict) or not rest or not set(rest) <= _STRAY_CLOSERS | _WHITESPACE:
            raise
        logger.warning(
            "tool_args.trailing_closers_ignored count=%d buf_len=%d",
            sum(ch in _STRAY_CLOSERS for ch in rest),
            len(raw),
        )
        return value

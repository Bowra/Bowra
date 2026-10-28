"""记忆重做 批3 —— FR1 下传：把记忆送到**每个执行体**（tech-design §4.4）。

今天只有 Lead 拿得到 `<remembered_about_user>`，而**报告正文的作者是 peer**
（`BOWHEAD_WRITE_REPORT` 默认 ON，peer 亲笔是主路；§2.1）。所以「KPI 用 ¥M」这类
呈现口径的服从率只有 17% —— 不是模型不听话，是**写那段文字的执行体从来没看见过这条记忆**。

## 为什么渲染在 turn 起点做一次

`ToolExecutionContext.memory_pack` 是 turn 起点建的**唯一**快照（批0④ / §3.2）。
执行体块也在那时候渲染好、挂进同一个快照 ⇒ 所有 peer 拿到的是**同一串字节**，
而且这串字节就在落库的快照里，`/memory/replay` 重放看到的与 live 注入的必然一致
（chat 不变量）。若改成每个 peer 各自现渲染，同一 turn 内两个 peer 就可能拿到不同的记忆
（排序里有 `last_confirmed_at`，中途一次 `remember` 就够让它们分叉）。

## 裁剪：v1 只有预算截断这一条（r3 撤掉 D6 的角色分档）

实测 9 个 system manifest 的 tool allow **8:1** 落同一档 ⇒ 分档不区分任何东西，
却要引入一个**按工具名硬匹配**的判据 —— 本仓已因工具改名让这类判据静默失效过一次。
所以排序只按 `confidence` 与 `last_confirmed_at`，超预算就截断并记指标。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any

from bowhead_core.memory_write_policy import CONFIDENCE_HIGH
from bowhead_core.prompt_injection_defense import sanitize_for_injection

logger = logging.getLogger(__name__)

#: Memory budget per executor (characters). ≈ 400–500 Chinese tokens.
#: Positioned after `extra_instructions` ⇒ no additional truncation of any cacheable prefix (§4.4 d).
PEER_MEMORY_BUDGET = 800

_HEADER = "<remembered_about_user>"
_FOOTER = "</remembered_about_user>"
_INTRO = "以下是用户的长期偏好/规则/口径。与你这一步相关时**直接照做**，不要反问、也不要等用户重复说明。"


def _confidence_rank(entry: Any) -> int:
    return 0 if str(getattr(entry, "confidence", "") or "") == CONFIDENCE_HIGH else 1


def _recency(entry: Any) -> str:
    return str(getattr(entry, "last_confirmed_at", "") or "")


def sort_for_injection(entries: list[Any]) -> list[Any]:
    """`confidence=high` 优先 → `last_confirmed_at` 新的优先（§4.4 c）。

    ★ **两趟稳定排序**，不在一个 key 里做「一升一降」。第一版用码点取补
    （`0x10FFFF - ord(ch)`）把时间戳倒过来，再拿 `\uffff` 当「空时间戳」的哨兵——
    可取补后的时间戳字符都在 `0x10FF..` 段，**比哨兵大**，于是未回填 `last_confirmed_at`
    的条目反而排到了同档**最前面**（该最后才对）。聪明写法藏了个 bug，实测才看见。
    `reverse=True` 的空串天然排最后，一行就说清楚。

    排序方向必须与截断方向一致：截断从**尾部**砍，所以排在后面的就是超预算时先丢的
    —— 低置信、久未确认的那些。
    """
    by_recency = sorted(entries, key=_recency, reverse=True)
    return sorted(by_recency, key=_confidence_rank)


def _entry_line(entry: Any) -> str:
    """一条记忆渲染成一行。**key 与 value 都要过注入防护**。

    ⚠ 条目的 key/value 是**用户可控**的，而这一行会被原样拼进每个执行体（以及报告主笔）
    的 system prompt。Lead 侧对同样的内容做了 `sanitize_for_injection`
    （`memory_pack.py`：entries 段）与 `wrap_user_content`（notes 段），
    执行体这条路此前**一个都没做** —— 一条值里写着 `</remembered_about_user>` + 指令的记忆，
    能在每个 peer 和报告主笔那里从记忆块里逃出去。2026-09-04 收口 review 逮到，
    执行体记忆沿用已完成策略分类的存储边界。
    """
    value = entry.parsed_value() if hasattr(entry, "parsed_value") else getattr(entry, "value", "")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return _neutralize_own_tags(sanitize_for_injection(f"- {getattr(entry, 'key', '')}: {text}"))


def _neutralize_own_tags(line: str) -> str:
    """中和内容里出现的**本块自己的定界标签**。

    ⚠ `sanitize_for_injection` 只转义越狱关键词，**不管定界符** —— 实测：
    值里写 `</remembered_about_user> …` 时，转义完那个闭合标签仍原样留在块内，
    足以让记忆块提前收尾、后面的内容变成「块外的指令」。
    定界标签是本模块自己拼的（`_HEADER`/`_FOOTER`），所以由本模块负责中和。
    """
    out = line
    for tag in (_FOOTER, _HEADER):
        out = re.sub(re.escape(tag), f"[[escaped:{tag}]]", out, flags=re.IGNORECASE)
    return out


def render_executor_block(
    entries: list[Any],
    *,
    budget: int = PEER_MEMORY_BUDGET,
) -> tuple[str, int]:
    """渲染给执行体的记忆段，返回 `(块文本, 被截掉的条数)`。空列表 → `("", 0)`。

    **不带 `[mem_xxx]` id**：执行体不写记忆（`remember` 只在 Lead 的 allow-list 里），
    给它 id 只会多烧 18 字符/条的预算，还可能诱导它去编一个 `supersedes`。
    id 是 Lead 与蒸馏侧的把手，不是每个 peer 的。
    """
    if not entries:
        return "", 0

    ordered = sort_for_injection(entries)
    lines: list[str] = []
    used = len(_HEADER) + len(_INTRO) + len(_FOOTER) + 3
    dropped = 0
    for entry in ordered:
        line = _entry_line(entry)
        if used + len(line) + 1 > budget:
            dropped += 1
            continue
        lines.append(line)
        used += len(line) + 1

    if not lines:
        return "", len(ordered)
    body = "\n".join([_HEADER, _INTRO, *lines, _FOOTER])
    return body, dropped


async def track_truncation(metrics_log: Any | None, *, user_id: str, dropped: int, kept: int) -> None:
    """截断记指标（复用既有指标名 `memory.budget_truncation`，§4.4 c）。"""
    if not dropped or metrics_log is None:
        return
    with contextlib.suppress(Exception):
        await metrics_log.track(
            "memory.budget_truncation",
            user_id=user_id,
            attrs={"sections": "executor", "dropped": dropped, "kept": kept},
        )


__all__ = ["PEER_MEMORY_BUDGET", "render_executor_block", "sort_for_injection", "track_truncation"]

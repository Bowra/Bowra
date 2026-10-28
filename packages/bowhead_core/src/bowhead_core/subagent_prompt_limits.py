"""自定义 / 团队 Agent 系统提示词的长度闸 —— 字符与 token 两道，单一出处。

## 为什么要两道，而且 token 那道才是真的

字符闸对不同语言的宽松度差 6 倍（cl100k 实测：纯中文 1.05 tok/char、我们这种中英混写
0.63、纯英文 0.17）。原先只有字符闸，于是同一个 15000 的数字给中文作者 15.8k token、
给英文作者 2.5k token —— "上限"在两种语言里根本不是同一件事。token 闸把口径统一。

字符闸保留，作用变成 tokenize 之前的廉价预检（避免拿一份超大文本去过编码器）。

## 数字是怎么来的（都是量出来的，不是拍的）

需求侧 = 我们自己写的生产 agent 提示词（`agents/prompts/system/*.md`，cl100k）：
canvas-writer 1086 / business-analyst 1959 / compliance-analyst 1974 / financial-analyst
2424 / consultant 2445 / kb-analyst 2696 / marketing-analyst 2677 / **compute 8831**；
Lead 本体约 11600（见 `context_assembly` 模块文档）。

- ``MAX_PROMPT_TOKENS = 16000`` ≈ 我们最大的 peer（compute 8831）的 1.8 倍、Lead 的
  1.4 倍。凡是我们自己写得出来的提示词都撞不到它。
- ``RECOMMENDED_PROMPT_TOKENS = 6000`` = `compute-agent-optimize` sprint 把 compute.md
  分层重写后的落点（5145 tok）向上取整。超过它不拦，只在 UI 上提示"把反例和长清单下沉
  到 skill，常驻面只留硬约束"—— 那正是那个 sprint 做的事。

成本侧的注记：提示词位于 `split_system_for_prefix` 稳定段的**最前**，线上是自动前缀缓存
（命中率实测 31.7%→67.0%），所以长提示词只在冷启动付全价；同一个 peer 的工具 schema
实测 7704 tok，与提示词同量级甚至更大 —— 常驻成本的大头不在这里。
"""

from __future__ import annotations

from bowhead_core.token_estimator import estimate_tokens

# Character precheck limits for personal custom subagents and team Agents. Runtime is identical (same runner,
# context window, and prefix cache); the difference only allows longer English/bilingual prompts for teams. The real gate is
# the language-independent token limit below.
MAX_PROMPT_CHARS_PERSONAL = 16000
MAX_PROMPT_CHARS_TEAM = 24000

MAX_PROMPT_TOKENS = 16000
RECOMMENDED_PROMPT_TOKENS = 6000


def system_prompt_tokens(system_prompt: str) -> int:
    """提示词的 token 估算（cl100k 软上限口径，与窗口守卫同一把尺）。"""
    return estimate_tokens(system_prompt)


def system_prompt_token_overflow(system_prompt: str) -> int | None:
    """超出 token 闸时返回实际 token 数，否则 None。

    只在写路径（创建 / 更新 / 发版）调用 —— 编码一份 2 万字的文本约几毫秒，放在保存
    这种低频操作上无所谓；**不要**放进 spawn 热路径。
    """
    tokens = system_prompt_tokens(system_prompt)
    return tokens if tokens > MAX_PROMPT_TOKENS else None


__all__ = [
    "MAX_PROMPT_CHARS_PERSONAL",
    "MAX_PROMPT_CHARS_TEAM",
    "MAX_PROMPT_TOKENS",
    "RECOMMENDED_PROMPT_TOKENS",
    "system_prompt_token_overflow",
    "system_prompt_tokens",
]

"""个人设置「答案风格」→ 生成面的注入（照语言轴的形状做）。

设置页四档写进 `user_settings.answer_style`，gateway 的 `_resolve_web_language_inputs`
按「请求 > 账号设置」把它回落进 `TurnRequest.user_profile`（`954c250e1`）。但那个 dict
到 2026-09-16 为止**全仓没有任何消费方** —— 语言轴早在 `e4ccdbd2c` 改走独立形参
（`user_profile_language`），dict 就没人读了。净效果是「存得进去、界面回显对、回答一个字
不变」，比直接报错更隐蔽。本模块补的就是消费侧。

设计上刻意与语言轴同形，不新造第四套状态、不加第二条并行判据：

- **判据只有一处** —— `resolve_answer_style(ctx)` 读 `ctx.user_profile`，两个渲染点共用；
- **两个渲染点与语言轴一一对应**：
  ③ prompt 末尾 directive（`build_answer_style_directive`）挂在 `subagent._legacy_run`
    那个共同必经点上 —— 内置 peer / 团队与用户自定义 subagent / **Lead** 都走它；
  ② peer 的 Sub-task context 行（`render_answer_style_line`），紧挨着
    `render_spawn_language_line` 那行 —— peer 看不见用户原话，结构化下传才靠得住。
  两处读**同一张规则表** `_STYLE_RULES`，所以不存在「Lead 与 peer 各写一份、只改一侧」
  的老毛病（那是本仓反复发作的形态）。
- **没设置就不注入**。用户没保存过这个键时返 `""`，prompt 逐字节不变 —— 设置页默认显示
  「详细」只是前端初值，不是服务端事实，别把它当默认值写进提示词。

取值集合以设置页的 select 四个选项为准（`components/settings/profile.tsx`）。这里做白名单
校验只是为了「不认识就不注入」，**不**回落到任一档：老账号里的存量值、或以后前端加档
而后端没跟上时，宁可不注入也不要按猜的那档改写用户的回答。
"""

from __future__ import annotations

from typing import Any, Final

# The settings select's four options (`profile.tsx`), in page order.
SUPPORTED_ANSWER_STYLES: Final[tuple[str, ...]] = (
    "concise",
    "detailed",
    "technical",
    "business",
)

_STYLE_LABELS: Final[dict[str, str]] = {
    "concise": "简洁",
    "detailed": "详细",
    "technical": "技术",
    "business": "商业",
}

# Executable rules for each style. **This table is the sole source**: both the final directive and peer Sub-task lines read it.
# Wording requirement (same lesson as `_SPAWN_LANGUAGE_LINES`): describe observable structure (length / sections /
# terminology / focus), not adjectives such as "be concise" that models cannot execute or verify.
_STYLE_RULES: Final[dict[str, str]] = {
    "concise": ("直接给结论，能一段说完就不分节：不写小标题、不做背景铺垫、不复述问题、不罗列推导过程。只在结论必需时附一两个关键数字。"),
    "detailed": ("把话说透：先给结论，再给依据与推导过程，并交代口径、边界与例外情况。内容超过三段时用小标题分节，让读者能跳读。"),
    "technical": (
        "面向看得懂数据的读者：写清指标口径、计算方式、涉及的字段/维度与数据处理细节，该用专业术语就用，必要时给出计算式；不要为了通俗而省掉口径。"
    ),
    "business": ("面向业务与管理读者：先给结论和它对生意的影响，再落到可执行的建议；少讲技术实现与字段细节，术语换成业务说法。"),
}

# Both render sites share this clause: explicit length/structure requests in **this message** override settings.
# (Language-axis batch 6 lesson: putting message priority in a subordinate clause fails to outweigh the main clause in all-Chinese prompts.)
_OVERRIDE_CLAUSE: Final[str] = "用户在本轮消息里若显式要求了别的长度或结构，以消息为准。"


def resolve_answer_style(parent_ctx: Any) -> str:
    """本轮生效的答案风格：`ctx.user_profile.answer_style` → 四档之一，否则 ""。

    ""（不注入）覆盖三种情况：没挂 ctx / 用户从没保存过这个设置 / 存量值不在四档里。
    **不造默认值** —— 见模块 docstring。
    """
    profile = getattr(parent_ctx, "user_profile", None)
    if not isinstance(profile, dict):
        return ""
    style = str(profile.get("answer_style") or "").strip().lower()
    return style if style in SUPPORTED_ANSWER_STYLES else ""


def build_answer_style_directive(style: str) -> str:
    """prompt 末尾的风格约束段；未知/空 → ""（不注入）。

    以双换行开头、末尾不带换行 —— 与 `build_language_directive` 同一形状，
    因为两者共用 `split_system_for_prefix` 的同一个尾段槽位。
    """
    rule = _STYLE_RULES.get(style)
    if rule is None:
        return ""
    return "\n".join(
        (
            "\n\n## 回答风格",
            f"- 用户在个人设置里选的是 **{_STYLE_LABELS[style]}**（answer_style={style}）：{rule}",
            f"- 这条只管**给用户看的文字怎么写**，不改变取数口径、数字与引用规范：该标的来源照标，该给的数字照给。{_OVERRIDE_CLAUSE}",
        ),
    )


def render_answer_style_line(style: str) -> str:
    """peer 的 Sub-task context 行；未知/空 → ""（不注入）。

    与 `render_spawn_language_line` 并排。peer 拿不到用户原话里的风格诉求
    （`user_verbatim` 那行有 300 字上限且定位在核对实体词），所以风格同样要结构化下传。
    """
    rule = _STYLE_RULES.get(style)
    if rule is None:
        return ""
    return f"- **answer_style**: {style}（用户个人设置：{_STYLE_LABELS[style]}）—— {rule}{_OVERRIDE_CLAUSE}"


__all__ = [
    "SUPPORTED_ANSWER_STYLES",
    "build_answer_style_directive",
    "render_answer_style_line",
    "resolve_answer_style",
]
